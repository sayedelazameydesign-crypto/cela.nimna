"""The shipped image must carry the runtime optionals (offline).

Regression tests: the Dockerfile used to `pip install .` (base deps only),
so a REDIS_URL/QDRANT_URL set by compose/k8s/Render silently fell back to
in-memory — the operator configured infra the image could not use. The image
now installs .[prod], and this pins that contract.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME_OPTIONALS = {"redis", "qdrant-client", "numpy", "prometheus-client"}


def test_dockerfile_installs_the_prod_extra():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r'pip install "\.\[prod\]"', dockerfile), \
        "Dockerfile must install the prod extra (redis/qdrant must work when configured)"


def test_prod_extra_covers_the_runtime_optionals():
    try:
        import tomllib
    except ImportError:  # pragma: no cover - python < 3.11
        import tomli as tomllib
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    prod = pyproject["project"]["optional-dependencies"]["prod"]
    names = {re.split(r"[<>=!~\s\[]", req, 1)[0] for req in prod}
    assert RUNTIME_OPTIONALS <= names, f"prod extra is missing: {RUNTIME_OPTIONALS - names}"


def test_requirements_and_prod_extra_agree_on_optionals():
    """requirements.txt (CI/dev) and .[prod] (image) must not drift apart."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - python < 3.11
        import tomli as tomllib
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    prod = pyproject["project"]["optional-dependencies"]["prod"]
    prod_specs = {re.split(r"\s+#", req, 1)[0].strip() for req in prod}
    req_text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    req_specs = {re.split(r"\s+#", line, 1)[0].strip()
                 for line in req_text.splitlines()
                 if line.strip() and not line.startswith("#")}
    for spec in prod_specs:
        name = re.split(r"[<>=!~\s\[]", spec, 1)[0]
        matches = [r for r in req_specs if re.split(r"[<>=!~\s\[]", r, 1)[0] == name]
        assert matches, f"{name} (prod extra) is missing from requirements.txt"
        assert spec in matches, \
            f"specifier drift for {name}: prod has {spec!r}, requirements has {matches}"
