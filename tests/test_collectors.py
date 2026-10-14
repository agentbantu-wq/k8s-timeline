"""Collector tests with a fake K8sClient (no network)."""

from datetime import datetime, timedelta, timezone

from k8s_timeline import format as fmt
from k8s_timeline.collectors import (classify_event, collect_deployments,
                                     collect_events, collect_nodes,
                                     collect_pods)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=2)


class FakeClient:
    def __init__(self, resources):
        self.resources = resources

    def list(self, resource_path, namespace=None, label_selector=None):
        return self.resources.get(resource_path, [])

    def get(self, path, params=None):
        return {"items": self.resources.get(path, [])}


def test_classify_event():
    assert classify_event("BackOff", "Warning") == "error"
    assert classify_event("Killing", "Warning") == "warning"
    assert classify_event("Scheduled", "Normal") == "info"


def test_collect_events_filters_by_time():
    fresh = {"lastTimestamp": "2026-10-14T11:00:00Z", "type": "Warning",
             "reason": "BackOff", "message": "back-off restarting",
             "count": 5, "involvedObject": {"kind": "Pod", "name": "api-1",
                                            "namespace": "prod"}}
    stale = {"lastTimestamp": "2026-10-14T08:00:00Z", "type": "Normal",
             "reason": "Scheduled", "message": "scheduled",
             "involvedObject": {"kind": "Pod", "name": "api-0"}}
    client = FakeClient({"events": [fresh, stale]})
    out = collect_events(client, "prod", SINCE)
    assert len(out) == 1
    assert out[0]["reason"] == "BackOff"
    assert out[0]["level"] == "error"
    assert out[0]["count"] == 5


def test_collect_pods_crashloop():
    pod = {"metadata": {"name": "api-1", "namespace": "prod"},
           "status": {"phase": "Running",
                      "containerStatuses": [{
                          "name": "api",
                          "restartCount": 7,
                          "state": {"waiting": {"reason": "CrashLoopBackOff",
                                               "message": "back-off 5m0s"}},
                          "lastState": {"terminated": {
                              "reason": "OOMKilled", "exitCode": 137,
                              "finishedAt": "2026-10-14T11:30:00Z"}},
                      }]}}
    client = FakeClient({"pods": [pod]})
    out = collect_pods(client, "prod", SINCE, NOW)
    reasons = {e["reason"] for e in out}
    assert "ContainerWaiting:CrashLoopBackOff" in reasons
    assert "ContainerTerminated:OOMKilled" in reasons
    assert all(e["level"] == "error" for e in out)


def test_collect_deployments_rollout():
    dep = {"metadata": {"name": "api", "namespace": "prod",
                        "annotations": {"deployment.kubernetes.io/revision": "12"}},
           "status": {"conditions": [{
               "type": "Progressing", "status": "True",
               "reason": "NewReplicaSetAvailable",
               "message": "replica set updated",
               "lastUpdateTime": "2026-10-14T11:15:00Z"}]}}
    rs = {"metadata": {"name": "api-7d9f", "namespace": "prod",
                       "creationTimestamp": "2026-10-14T11:14:00Z",
                       "annotations": {"deployment.kubernetes.io/revision": "12"}},
          "status": {"replicas": 3}}
    client = FakeClient({
        "/apis/apps/v1/namespaces/prod/deployments": [dep],
        "/apis/apps/v1/namespaces/prod/replicasets": [rs],
    })
    out = collect_deployments(client, "prod", SINCE)
    assert len(out) == 2
    assert any(e["kind"] == "Deployment" for e in out)
    assert any(e["reason"] == "ReplicaSetCreated" for e in out)


def test_collect_nodes_flags_pressure():
    node = {"metadata": {"name": "node-1"},
            "status": {"conditions": [
                {"type": "Ready", "status": "True",
                 "lastTransitionTime": "2026-10-01T00:00:00Z"},
                {"type": "MemoryPressure", "status": "True",
                 "message": "kubelet has memory pressure",
                 "lastTransitionTime": "2026-10-14T11:45:00Z"},
            ]}}
    client = FakeClient({"nodes": [node]})
    out = collect_nodes(client, SINCE, NOW)
    assert len(out) == 1
    assert out[0]["level"] == "warning"
    assert "MemoryPressure" in out[0]["reason"]


def test_pmc_export_shape():
    entries = [{
        "timestamp": NOW, "category": "event", "kind": "Pod", "name": "api-1",
        "namespace": "prod", "level": "error", "reason": "BackOff",
        "message": "back-off restarting", "count": 3,
    }]
    import json
    doc = json.loads(fmt.to_postmortem_copilot(entries, service="api"))
    assert doc["service"] == "api"
    assert doc["events"][0]["source"] == "k8s:event"
    assert "BackOff" in doc["events"][0]["message"]
