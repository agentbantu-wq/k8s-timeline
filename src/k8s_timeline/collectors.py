"""Collectors: pull raw cluster state and turn it into timeline entries.

Each collector returns a list of raw entry dicts with at least:
    timestamp (datetime), category, kind, name, namespace, level, reason, message

Levels: info | warning | error. Categories: event | pod | deployment | node.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .k8s import K8sClient


def parse_rfc3339(value: str) -> datetime | None:
    """Parse the timestamp flavors the Kubernetes API emits."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Reasons that almost always mean something broke (not just informational).
ERROR_REASONS = {
    "Failed", "FailedMount", "FailedAttachVolume", "FailedScheduling",
    "FailedCreate", "FailedDelete", "FailedValidation", "BackOff",
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "ImagePullFailed",
    "OOMKilled", "ContainerCannotRun", "DeadlineExceeded", "Evicted",
    "Preempted", "NodeNotReady", "Unhealthy", "FailedSync", "FailedMapVolume",
    "ProvisioningFailed", "FailedBinding", "VolumeFailed",
}

WARNING_REASONS = {
    "Killing", "Pulled", "Started", "Created", "Scheduled", "PulledImage",
    "SuccessfulCreate", "SuccessfulDelete", "ScalingReplicaSet",
}


def classify_event(reason: str, event_type: str) -> str:
    """Map a k8s event to info/warning/error."""
    if reason in ERROR_REASONS:
        return "error"
    if event_type == "Warning":
        return "warning"
    return "info"


def collect_events(client: K8sClient, namespace: str | None,
                   since: datetime) -> list[dict]:
    """Collect core/v1 Events at or after `since`."""
    items = client.list("events", namespace=namespace)
    entries = []
    for ev in items:
        ts = parse_rfc3339(ev.get("lastTimestamp") or ev.get("eventTime") or "")
        if ts is None or ts < since:
            continue
        obj = ev.get("involvedObject", {}) or {}
        entries.append({
            "timestamp": ts,
            "category": "event",
            "kind": obj.get("kind", "?"),
            "name": obj.get("name", "?"),
            "namespace": obj.get("namespace") or namespace or "",
            "level": classify_event(ev.get("reason", ""), ev.get("type", "Normal")),
            "reason": ev.get("reason", ""),
            "message": (ev.get("message") or "").strip(),
            "count": ev.get("count", 1),
        })
    return entries


def _container_state_entries(pod: dict) -> list[dict]:
    """Turn waiting/terminated container states into entries."""
    entries = []
    name = pod["metadata"]["name"]
    ns = pod["metadata"].get("namespace", "")
    for cs in (pod.get("status", {}) or {}).get("containerStatuses", []) or []:
        cname = cs.get("name", "?")
        restarts = cs.get("restartCount", 0)
        state = cs.get("state", {}) or {}
        waiting = state.get("waiting") or {}
        if waiting:
            reason = waiting.get("reason", "")
            level = "error" if reason in ERROR_REASONS else "warning"
            entries.append({
                "timestamp": None,  # filled by caller with collection time
                "category": "pod",
                "kind": "Pod",
                "name": f"{name}/{cname}",
                "namespace": ns,
                "level": level,
                "reason": f"ContainerWaiting:{reason}",
                "message": (f"container {cname} waiting ({reason}): "
                            f"{waiting.get('message', '').strip()} "
                            f"[restarts={restarts}]").strip(),
                "count": restarts,
            })
        terminated = (cs.get("lastState", {}) or {}).get("terminated") or {}
        if terminated:
            reason = terminated.get("reason", "")
            exit_code = terminated.get("exitCode", "?")
            level = "error" if reason in ERROR_REASONS or exit_code not in (0, "0") else "info"
            finished = parse_rfc3339(terminated.get("finishedAt") or "")
            entries.append({
                "timestamp": finished,
                "category": "pod",
                "kind": "Pod",
                "name": f"{name}/{cname}",
                "namespace": ns,
                "level": level,
                "reason": f"ContainerTerminated:{reason or 'Completed'}",
                "message": (f"container {cname} last terminated "
                            f"(reason={reason or 'Completed'}, exit={exit_code})").strip(),
                "count": restarts,
            })
    return entries


def collect_pods(client: K8sClient, namespace: str | None,
                 since: datetime, now: datetime) -> list[dict]:
    """Collect pod-level signals: crashloops, OOMKills, evictions, phase changes."""
    pods = client.list("pods", namespace=namespace)
    entries = []
    for pod in pods:
        meta = pod.get("metadata", {}) or {}
        status = pod.get("status", {}) or {}
        for entry in _container_state_entries(pod):
            if entry["timestamp"] is None:
                entry["timestamp"] = now  # current-state signal
            if entry["timestamp"] >= since:
                entries.append(entry)
        phase = status.get("phase", "")
        if phase in ("Failed", "Unknown", "Pending"):
            # Pending older than the window start is worth flagging (stuck scheduling).
            created = parse_rfc3339(meta.get("creationTimestamp") or "")
            if phase == "Pending" and created and created >= since:
                pass  # fresh pending pods are normal; skip
            elif phase == "Pending":
                entries.append({
                    "timestamp": created or now,
                    "category": "pod",
                    "kind": "Pod",
                    "name": meta.get("name", "?"),
                    "namespace": meta.get("namespace", ""),
                    "level": "warning",
                    "reason": "PodStuckPending",
                    "message": f"pod stuck in Pending since {created}",
                    "count": 1,
                })
            else:
                entries.append({
                    "timestamp": now,
                    "category": "pod",
                    "kind": "Pod",
                    "name": meta.get("name", "?"),
                    "namespace": meta.get("namespace", ""),
                    "level": "error",
                    "reason": f"PodPhase:{phase}",
                    "message": f"pod phase is {phase}: {(status.get('message') or '').strip()}",
                    "count": 1,
                })
    return entries


def collect_deployments(client: K8sClient, namespace: str | None,
                        since: datetime) -> list[dict]:
    """Reconstruct rollout activity from Deployments, ReplicaSets and their revisions."""
    entries = []
    # apps/v1 lives outside core; the client's list() targets /api/v1, so use get().
    def apps_list(resource: str) -> list:
        if namespace:
            path = f"/apis/apps/v1/namespaces/{namespace}/{resource}"
        else:
            path = f"/apis/apps/v1/{resource}"
        return client.get(path).get("items", [])

    for dep in apps_list("deployments"):
        meta = dep.get("metadata", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        revision = annotations.get("deployment.kubernetes.io/revision", "?")
        created = parse_rfc3339(meta.get("creationTimestamp") or "")
        for cond in (dep.get("status", {}) or {}).get("conditions", []) or []:
            ts = parse_rfc3339(cond.get("lastUpdateTime") or cond.get("lastTransitionTime") or "")
            if ts is None or ts < since:
                continue
            if cond.get("type") == "Progressing" and cond.get("reason") == "NewReplicaSetAvailable":
                level = "info"
            elif cond.get("status") == "False":
                level = "warning"
            else:
                level = "info"
            entries.append({
                "timestamp": ts,
                "category": "deployment",
                "kind": "Deployment",
                "name": meta.get("name", "?"),
                "namespace": meta.get("namespace", ""),
                "level": level,
                "reason": f"Deployment{cond.get('type')}:{cond.get('reason', '')}",
                "message": (f"deployment revision {revision}: "
                            f"{cond.get('type')}={cond.get('status')} "
                            f"({(cond.get('message') or '').strip()})").strip(),
                "count": 1,
            })
        _ = created  # reserved for future age-based heuristics

    for rs in apps_list("replicasets"):
        meta = rs.get("metadata", {}) or {}
        annotations = meta.get("annotations", {}) or {}
        created = parse_rfc3339(meta.get("creationTimestamp") or "")
        if created is None or created < since:
            continue
        revision = annotations.get("deployment.kubernetes.io/revision", "?")
        replicas = (rs.get("status", {}) or {}).get("replicas", 0)
        entries.append({
            "timestamp": created,
            "category": "deployment",
            "kind": "ReplicaSet",
            "name": meta.get("name", "?"),
            "namespace": meta.get("namespace", ""),
            "level": "info",
            "reason": "ReplicaSetCreated",
            "message": (f"replicaset created (deployment revision {revision}, "
                        f"replicas={replicas})"),
            "count": 1,
        })
    return entries


def collect_nodes(client: K8sClient, since: datetime, now: datetime) -> list[dict]:
    """Flag nodes with bad conditions (pressure, NotReady)."""
    nodes = client.list("nodes")
    entries = []
    for node in nodes:
        name = (node.get("metadata", {}) or {}).get("name", "?")
        for cond in (node.get("status", {}) or {}).get("conditions", []) or []:
            ctype = cond.get("type", "")
            status = cond.get("status", "")
            bad = (ctype == "Ready" and status != "True") or (
                ctype in ("MemoryPressure", "DiskPressure", "PIDPressure",
                          "NetworkUnavailable") and status == "True"
            )
            if not bad:
                continue
            ts = parse_rfc3339(cond.get("lastTransitionTime") or "") or now
            if ts < since:
                continue
            entries.append({
                "timestamp": ts,
                "category": "node",
                "kind": "Node",
                "name": name,
                "namespace": "",
                "level": "error" if ctype == "Ready" else "warning",
                "reason": f"NodeCondition:{ctype}={status}",
                "message": (f"node condition {ctype}={status}: "
                            f"{(cond.get('message') or '').strip()}").strip(),
                "count": 1,
            })
    return entries
