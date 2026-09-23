"""
nimna.api.app
-------------

FastAPI (or Flask) based HTTP API entry point. The health endpoint now reports
the status of the anomaly detector.
"""

import os
from typing import Dict

# The original project uses FastAPI; we keep the same import style.
try:
    from fastapi import FastAPI
except ImportError:  # pragma: no cover
    # Fallback stub for environments where FastAPI is not installed.
    from flask import Flask as FastAPI  # type: ignore

app = FastAPI(title="Nimna API")

# --------------------------------------------------------------------- #
# Health endpoint – existing implementation (simplified)
# --------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> Dict[str, str]:
    """
    Basic health check – always returns ``OK``.
    """
    return {"status": "OK"}

# --------------------------------------------------------------------- #
# New anomaly‑specific health endpoint
# --------------------------------------------------------------------- #
@app.get("/api/health.anomaly")
def health_anomaly() -> Dict[str, object]:
    """
    Report whether the anomaly detector is enabled and which heuristic version
    is active.
    """
    # In a full implementation the detector could be a singleton; here we just
    # report static information.
    enabled = os.getenv("ANOMALY_DETECTOR_ENABLED", "true").lower() == "true"
    detector_name = "heuristics-v1"
    return {"enabled": enabled, "detector": detector_name}
