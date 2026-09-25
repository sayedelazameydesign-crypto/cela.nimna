"""FOSS-only stack posture (offline — manifest + config assertions).

Pins the 2026-09-25 license-compliance slice:
  * the RESP cache is Valkey (BSD-3), not Redis >= 7.4 (SSPL/RSAL) —
    compose, k8s, server binary, and healthcheck all agree;
  * the HAProxy / Prometheus / pgBackRest example configs exist and
    reference the app's real endpoints and metric names;
  * docs/FREE-STACK.md records the verification verdicts (adopted vs
    rejected tools) so a stale recommendation fails loudly.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

VALKEY_IMAGE = "valkey/valkey:8"


def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


def _k8s_docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((REPO / "k8s" / name).read_text(encoding="utf-8")) if d]


def test_compose_cache_is_valkey_not_redis():
    redis_svc = _compose()["services"]["redis"]
    assert redis_svc["image"] == VALKEY_IMAGE
    assert redis_svc["command"][0] == "valkey-server"
    assert "valkey-cli" in redis_svc["healthcheck"]["test"]
    assert "redis-cli" not in " ".join(redis_svc["healthcheck"]["test"])


def test_no_restricted_redis_image_anywhere():
    for path in (REPO / "docker-compose.yml", REPO / "k8s" / "redis.yaml"):
        text = path.read_text(encoding="utf-8")
        for banned in ("image: redis:", "image: redis@", "image: redislabs/"):
            assert banned not in text, f"{banned} in {path.name}"
        assert "redis-server" not in text, f"redis-server binary in {path.name}"


def test_k8s_cache_is_valkey():
    images, commands = [], []
    for doc in _k8s_docs("redis.yaml"):
        if doc.get("kind") == "Deployment":
            for container in doc["spec"]["template"]["spec"]["containers"]:
                images.append(container["image"])
                commands.append(container["command"][0])
    assert images == [VALKEY_IMAGE]
    assert commands == ["valkey-server"]


def test_valkey_conf_present_and_bounded():
    conf = REPO / "infra" / "redis" / "redis.conf"
    text = conf.read_text(encoding="utf-8")
    assert "maxmemory 256mb" in text
    assert "maxmemory-policy allkeys-lru" in text
    assert "Valkey" in text  # header says who actually reads this file


def test_haproxy_example_checks_the_real_health_endpoint():
    cfg = (REPO / "infra" / "haproxy" / "haproxy.cfg").read_text(encoding="utf-8")
    assert "option httpchk GET /api/health" in cfg
    assert cfg.count("server nimna") >= 2  # at least two backends, else no LB
    assert "balance leastconn" in cfg


def test_prometheus_scrapes_the_authed_metrics_endpoint():
    prom = yaml.safe_load((REPO / "infra" / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))
    jobs = {job["job_name"]: job for job in prom["scrape_configs"]}
    assert jobs["nimna"]["metrics_path"] == "/api/metrics"
    # /api/metrics is behind API-key auth — the scraper must send a token,
    # and never inline it in the config file.
    auth = jobs["nimna"]["authorization"]
    assert auth["type"] == "Bearer"
    assert auth["credentials_file"].endswith("nimna_metrics_token")
    assert "alerts.yml" in prom["rule_files"]


def test_alerts_reference_real_metric_names():
    alerts = yaml.safe_load((REPO / "infra" / "monitoring" / "alerts.yml").read_text(encoding="utf-8"))
    exprs = " ".join(rule["expr"] for group in alerts["groups"] for rule in group["rules"])
    # names must match nimna/api/telemetry.py + security.py — a rule on a
    # nonexistent metric never fires and lies in the runbook.
    for real in ("http_requests_total", "http_request_duration_seconds_sum",
                 "http_request_duration_seconds_count", "breaker_transitions_total",
                 "rate_limiter_errors_total"):
        assert real in exprs, real
    assert "http_request_duration_seconds_bucket" not in exprs  # summary, not histogram


def test_pgbackrest_example_covers_backup_and_restore():
    text = (REPO / "infra" / "postgres" / "pgbackrest.conf.example").read_text(encoding="utf-8")
    for needle in ("pgbackrest --stanza=main backup", "pgbackrest --stanza=main restore",
                   "archive-push", "retention-full"):
        assert needle in text, needle


def test_free_stack_doc_records_verdicts():
    doc = (REPO / "docs" / "FREE-STACK.md").read_text(encoding="utf-8")
    for needle in ("Valkey", "valkey/valkey:8", "Orion", "bit2swaz/orion", "StrikeMQ", "Rafka", "LucidMQ",
                   "brokerless", "NATS JetStream", "pgBackRest", "OpenTofu", "SigNoz", "PikoCI",
                   "Gitness", "Krkn", "Infisical", "TFeatureSet", "PolyForm", "harness/harness", "GPL-3.0", "pin-to-tag", "LTS", "v15.0", "D1", "100K", "Restic", "BorgBackup", "re-verify"):
        assert needle in doc, needle
