"""Collector: aggregates VisionCache hit_rate, Vector counts, Anomaly kills, Swarm stats.

Exposed via GET /api/analytics/summary (added in api/app.py when available).
For scaffold, data is in-memory + pulled from stores on demand.
"""
from __future__ import annotations
import time
from typing import Any

class AnalyticsCollector:
    def __init__(self):
        self.started = time.time()
    def summary(self) -> dict[str, Any]:
        # lazy imports to avoid hard deps
        cache = {}
        try:
            from nimna.vision.cache import get_vision_cache
            cache = get_vision_cache().stats()
        except Exception as e:
            cache = {"error": str(e)[:100]}
        memory = {}
        try:
            from nimna.memory.qdrant import get_vector_memory
            memory = get_vector_memory().health()
        except Exception as e:
            memory = {"error": str(e)[:100]}
        swarm = {}
        try:
            from nimna.config import Settings
            s = Settings.from_env()
            swarm = {"enabled": s.swarm_enabled, "max_agents": s.swarm_max_agents}
        except Exception:
            pass
        # audit stats (last 100 events)
        audit_hist = {}
        try:
            from nimna.memory.store import MemoryStore
            from nimna.config import Settings
            s = Settings.from_env()
            store = MemoryStore(s.db_path)
            events = store.get_audit(limit=100)
            from collections import Counter
            c = Counter([e["event"] for e in events])
            audit_hist = dict(c.most_common(10))
        except Exception:
            audit_hist = {}
        return {
            "uptime_s": int(time.time() - self.started),
            "cache": cache,
            "memory": {"provider": memory.get("provider"), "counts": memory.get("counts")},
            "swarm": swarm,
            "audit_top_events": audit_hist,
            "generated_at": time.time(),
        }

_singleton=None
def get_collector():
    global _singleton
    if _singleton is None:
        _singleton=AnalyticsCollector()
    return _singleton
