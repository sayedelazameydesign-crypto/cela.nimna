"""Heuristics Kill Switch — MVP for Anomaly Detection.

Monitors Terminal Logs (shell_execute stdout/stderr) in real-time.
If suspicious patterns exceed thresholds, should_kill() returns True and the agent
must abort the session (audit + close).

This is the heuristic MVP before ML — fast, no model needed, immediate protection.

Rules (tuned):
- curl/wget piped to shell (already blocked, but double-check if bypassed)
- chmod +s / chown / iptables / mkfs / dd
- Repeated rm -rf attempts (≥2 in 5m)
- Escape workspace (../../ or /etc/passwd, /proc/self)
- Fork bomb or pty allocation attempts
- High frequency: ≥8 shell calls in 30s
- Network exfiltration: 3+ curl/wget to external hosts in 1m

Usage:
    from security.anomaly import get_detector
    det = get_detector()
    if det.should_kill(session_id, logs):
        await kill_session(session_id)

Integrates with agent: call in _run_tool after shell_execute.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Any

# Global sliding windows per session
_CALLS: dict[str, deque] = defaultdict(lambda: deque(maxlen=32))
_RM_ATTEMPTS: dict[str, deque] = defaultdict(lambda: deque(maxlen=16))

_PATTERNS = [
    (re.compile(r"curl\s+.*\|\s*(bash|sh|zsh)", re.I), "curl piped to shell", 100),
    (re.compile(r"wget\s+.*\|\s*(bash|sh|zsh)", re.I), "wget piped to shell", 100),
    (re.compile(r"base64\s+[^|]*\|\s*(bash|sh)", re.I), "base64 piped to shell", 100),
    (re.compile(r"chmod\s+\+s", re.I), "chmod +s (setuid)", 100),
    (re.compile(r"\b(chown|iptables|mkfs|mkswap|fdisk|dd\s+if=)", re.I), "privileged disk/net", 100),
    (re.compile(r":\(\)\s*\{\s*:\|\:&\s*;\s*\}"), "fork bomb", 100),
    (re.compile(r"\b(screen|tmux|pty|nohup|ssh\s+|ncat|nc\s+-l|socat)", re.I), "interactive escape", 80),
    (re.compile(r"(\.\./){2,}"), "path traversal ../../", 80),
    (re.compile(r"/etc/(passwd|shadow)"), "access /etc/passwd", 90),
    (re.compile(r"/proc/self"), "access /proc/self", 80),
    (re.compile(r"rm\s+-rf\s+/", re.I), "rm -rf /", 100),
]

class AnomalyDetector:
    def __init__(self, window_s: int = 60, max_calls: int = 8):
        self.window_s = window_s
        self.max_calls = max_calls

    def _check_patterns(self, text: str) -> tuple[bool, str, int]:
        for pat, name, score in _PATTERNS:
            if pat.search(text):
                return True, name, score
        return False, "", 0

    def log_call(self, session_id: str, tool: str, args: dict[str, Any], stdout: str = "", stderr: str = "") -> None:
        now = time.time()
        _CALLS[session_id].append((now, tool, str(args)[:500], stdout[:500], stderr[:200]))
        if tool == "shell_execute":
            cmd = str(args.get("command", ""))[:1000]
            hay = f"{cmd} {stdout} {stderr}"
            hit, name, _ = self._check_patterns(hay)
            if hit and "rm -rf" in hay.lower():
                _RM_ATTEMPTS[session_id].append(now)

    def should_kill(self, session_id: str, recent_logs: str | None = None) -> tuple[bool, str]:
        now = time.time()
        calls = _CALLS.get(session_id, deque())
        # frequency: 8+ shell calls in 30s
        recent = [c for c in calls if now - c[0] < 30 and c[1] == "shell_execute"]
        if len(recent) >= self.max_calls:
            return True, f"high frequency: {len(recent)} shell calls in 30s (limit {self.max_calls})"
        # rm -rf repeated
        rms = [t for t in _RM_ATTEMPTS.get(session_id, []) if now - t < 300]
        if len(rms) >= 2:
            return True, f"repeated rm -rf attempts ({len(rms)} in 5m)"
        # pattern on recent logs
        if recent_logs:
            hit, name, score = self._check_patterns(recent_logs)
            if hit and score >= 80:
                return True, f"heuristic match: {name} (score {score})"
        # network exfiltration: 3+ curl/wget in 60s
        curls = [c for c in calls if now - c[0] < 60 and any(k in str(c[2]).lower() for k in ["curl ", "wget "] )]
        if len(curls) >= 3:
            return True, f"possible exfiltration: {len(curls)} curl/wget in 60s"
        return False, ""

    def reset(self, session_id: str) -> None:
        _CALLS.pop(session_id, None)
        _RM_ATTEMPTS.pop(session_id, None)

_singleton: AnomalyDetector | None = None

def get_detector() -> AnomalyDetector:
    global _singleton
    if _singleton is None:
        _singleton = AnomalyDetector()
    return _singleton

def reset_detector() -> None:
    global _singleton
    _singleton = None
    _CALLS.clear()
    _RM_ATTEMPTS.clear()
