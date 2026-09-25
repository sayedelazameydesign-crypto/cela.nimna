"""Kubernetes resilience posture (offline — manifest assertions).

Pins the no-SPOF basics: replicas spread across nodes/zones, voluntary
disruptions budgeted, health probes on the real endpoint, rolling updates
that never go to zero, and an HPA that targets this Deployment.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _docs(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((REPO / "k8s" / name).read_text(encoding="utf-8")) if d]


def _deployment() -> dict:
    for doc in _docs("deployment.yaml"):
        if doc.get("kind") == "Deployment":
            return doc
    raise AssertionError("Deployment missing from k8s/deployment.yaml")


def test_all_manifests_parse():
    for manifest in ("deployment.yaml", "hpa.yaml", "qdrant.yaml", "redis.yaml"):
        assert _docs(manifest), manifest


def test_replicas_and_rolling_update_never_go_to_zero():
    deployment = _deployment()
    assert deployment["spec"]["replicas"] >= 2
    strategy = deployment["spec"]["strategy"]
    assert strategy["type"] == "RollingUpdate"
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0


def test_pod_anti_affinity_spreads_across_nodes_and_zones():
    pod_spec = _deployment()["spec"]["template"]["spec"]
    terms = pod_spec["affinity"]["podAntiAffinity"][
        "preferredDuringSchedulingIgnoredDuringExecution"]
    keys = {t["podAffinityTerm"]["topologyKey"] for t in terms}
    assert "kubernetes.io/hostname" in keys
    assert "topology.kubernetes.io/zone" in keys
    for term in terms:
        selector = term["podAffinityTerm"]["labelSelector"]["matchLabels"]
        assert selector == _deployment()["spec"]["selector"]["matchLabels"]


def test_pod_disruption_budget_keeps_one_serving():
    pdbs = [d for d in _docs("deployment.yaml") if d.get("kind") == "PodDisruptionBudget"]
    assert len(pdbs) == 1
    pdb = pdbs[0]
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"] == _deployment()["spec"]["selector"]["matchLabels"]


def test_probes_hit_the_real_health_endpoint():
    container = _deployment()["spec"]["template"]["spec"]["containers"][0]
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == "/api/health"
    assert _deployment()["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] >= 30


def test_hpa_targets_the_deployment():
    hpas = [d for d in _docs("hpa.yaml") if d.get("kind") == "HorizontalPodAutoscaler"]
    assert hpas, "HPA manifest missing"
    assert hpas[0]["spec"]["scaleTargetRef"]["name"] == _deployment()["metadata"]["name"]
    assert hpas[0]["spec"]["minReplicas"] >= 2


def test_metrics_annotation_points_at_the_authed_endpoint():
    annotations = _deployment()["spec"]["template"]["metadata"].get("annotations", {})
    assert annotations.get("prometheus.io/path") == "/api/metrics"
