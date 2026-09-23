"""
Optional pytest configuration – ensures that missing optional services (e.g.
Redis) do not cause import errors for the test suite.
"""

import os

def pytest_configure(config):
    # Disable any environment‑specific flags that would require external services.
    os.environ.setdefault("REDIS_URL", "")
