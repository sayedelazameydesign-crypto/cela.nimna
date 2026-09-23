"""
security.anomaly
----------------

Heuristic based anomaly detection for shell command patterns.

The detector tracks:
* Known dangerous command patterns (e.g., `curl|sh`, `chmod +s`, etc.).
* High‑frequency command execution ( >8 calls in 30 s ).
* Potential exfiltration via repeated `curl` usage ( >3 calls in 60 s ).

It is deliberately lightweight – no external services are required – and can be
instantiated per‑session.
"""

import re
import time
from collections import deque
from typing import Deque, List, Tuple


class AnomalyDetector:
    """
    Detects anomalous shell activity based on a set of heuristics.

    Usage
    -----
    >>> detector = AnomalyDetector()
    >>> detector.log_call("curl|bash")
    >>> detector.should_kill()
    True
    """

    # --------------------------------------------------------------------- #
    # Pattern definitions (regex strings)
    # --------------------------------------------------------------------- #
    _PATTERNS: List[Tuple[str, re.Pattern]] = [
        ("curl_pipe_sh", re.compile(r"curl\s*\|\s*sh", re.IGNORECASE)),
        ("chmod_suid", re.compile(r"chmod\s+\+s", re.IGNORECASE)),
        ("rm_rf_root", re.compile(r"rm\s+-rf\s+/", re.IGNORECASE)),
        ("fork_bomb", re.compile(r":\s*\(\s*\)\s*{\s*:\s*\|\s*:\s*}&", re.IGNORECASE)),
        ("path_traversal", re.compile(r"\.\./\.\./", re.IGNORECASE)),
        ("etc_passwd", re.compile(r"/etc/passwd", re.IGNORECASE)),
        ("proc_self", re.compile(r"/proc/self", re.IGNORECASE)),
    ]

    # --------------------------------------------------------------------- #
    # Frequency thresholds
    # --------------------------------------------------------------------- #
    _HIGH_FREQ_LIMIT = 8          # calls
    _HIGH_FREQ_WINDOW = 30.0      # seconds
    _EXFIL_LIMIT = 3              # curl calls
    _EXFIL_WINDOW = 60.0          # seconds

    def __init__(self) -> None:
        # Deques store timestamps (float seconds since epoch)
        self._call_times: Deque[float] = deque()
        self._curl_times: Deque[float] = deque()
        self._kill_flag: bool = False

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def log_call(self, command: str, timestamp: float | None = None) -> None:
        """
        Record a command execution and evaluate heuristics.

        Parameters
        ----------
        command: str
            The raw command string that was executed.
        timestamp: float | None
            Optional epoch timestamp (useful for testing). If omitted,
            ``time.time()`` is used.
        """
        ts = timestamp if timestamp is not None else time.time()
        self._call_times.append(ts)
        self._prune_old(self._call_times, self._HIGH_FREQ_WINDOW, ts)

        # Track curl usage for exfiltration heuristic
        if "curl" in command.lower():
            self._curl_times.append(ts)
            self._prune_old(self._curl_times, self._EXFIL_WINDOW, ts)

        # Pattern matching – any match triggers immediate kill
        for name, regex in self._PATTERNS:
            if regex.search(command):
                # Debug hook – could be replaced with proper logging
                # print(f"[AnomalyDetector] Pattern matched: {name}")
                self._kill_flag = True
                return  # immediate kill, no need to evaluate further

        # Frequency based heuristics
        if len(self._call_times) > self._HIGH_FREQ_LIMIT:
            # print("[AnomalyDetector] High frequency command execution detected")
            self._kill_flag = True
            return

        if len(self._curl_times) > self._EXFIL_LIMIT:
            # print("[AnomalyDetector] Potential exfiltration via curl detected")
            self._kill_flag = True
            return

    def should_kill(self) -> bool:
        """
        Return ``True`` if any heuristic indicates the session should be terminated.
        """
        return self._kill_flag

    # --------------------------------------------------------------------- #
    # Helper utilities
    # --------------------------------------------------------------------- #
    @staticmethod
    def _prune_old(queue: Deque[float], window: float, now: float) -> None:
        """
        Remove timestamps older than ``window`` seconds from ``queue``.
        """
        while queue and (now - queue[0] > window):
            queue.popleft()
