"""
nimna.core.agent
----------------

Core agent implementation. The _run_tool method is responsible for executing
external tools (including shell commands). This patch integrates the
AnomalyDetector to automatically kill a session when a dangerous pattern is
observed.
"""

import subprocess
import logging
from typing import Any, Dict

# Local imports
from security.anomaly import AnomalyDetector

logger = logging.getLogger(__name__)

class Agent:
    """
    Simplified representation of the Agent. In the real project this class
    contains many more responsibilities (state handling, tool registry, etc.).
    """

    def __init__(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.active = True                     # Session liveness flag
        self.anomaly_detector = AnomalyDetector()
        # Placeholder for an audit logger; in the real codebase this would be
        # a proper structured logger or external service.
        self.audit_log: list[Dict[str, Any]] = []

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def _run_tool(self, command: str) -> str:
        """
        Execute a shell command, log the call, and enforce the kill‑switch if
        an anomaly is detected.

        Parameters
        ----------
        command: str
            The command line to execute.

        Returns
        -------
        str
            The stdout of the executed command (empty string if killed).
        """
        if not self.active:
            logger.warning("Attempted to run tool on inactive session %s", self.session_id)
            return ""

        # -----------------------------------------------------------------
        # 1. Execute the command safely
        # -----------------------------------------------------------------
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )
            output = result.stdout
        except Exception as exc:  # pragma: no cover – defensive
            logger.exception("Shell execution failed: %s", exc)
            output = ""

        # -----------------------------------------------------------------
        # 2. Record the call for anomaly detection
        # -----------------------------------------------------------------
        self.anomaly_detector.log_call(command)

        # -----------------------------------------------------------------
        # 3. Evaluate kill‑switch
        # -----------------------------------------------------------------
        if self.anomaly_detector.should_kill():
            self._audit_event("anomaly_kill", {"command": command})
            self._kill_session()
            # Return a clear message to the caller
            return "[KILL SWITCH] Session terminated due to security anomaly."

        # -----------------------------------------------------------------
        # 4. Normal return
        # -----------------------------------------------------------------
        return output

    # --------------------------------------------------------------------- #
    # Internal helpers
    # --------------------------------------------------------------------- #
    def _audit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """
        Record an audit event. In production this would forward to a logging
        service or persistent store.
        """
        entry = {
            "session_id": self.session_id,
            "event": event_type,
            "payload": payload,
        }
        self.audit_log.append(entry)
        logger.info("Audit event: %s", entry)

    def _kill_session(self) -> None:
        """
        Deactivate the session. All subsequent tool invocations will be ignored.
        """
        self.active = False
        logger.warning("Session %s killed by AnomalyDetector.", self.session_id)
