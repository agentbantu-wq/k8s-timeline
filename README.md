# k8s-timeline

Reconstruct a Kubernetes incident timeline straight from cluster state — no agents, no exporters, no dependencies. Point it at a namespace and a time window; it pulls events, pod/container signals, rollout activity and node conditions, then emits one unified, chronologically ordered timeline.

**Stdlib-only Python. Zero dependencies.** It talks to the Kubernetes API with `http.client` + `ssl` and parses kubeconfig with a small built-in YAML-subset parser.

## Install

```bash
pip install k8s-timeline
# or run from source:
python -m k8s_timeline.cli --help
```

## Quick start

```bash
# Check connectivity and RBAC first
k8s-timeline doctor -n prod

# Build a timeline of the last 2 hours in prod
k8s-timeline build -n prod --since 2h

# Markdown report to a file
k8s-timeline build -n prod --since 30m --format markdown -o incident.md

# All namespaces, last day, JSON for scripting
k8s-timeline build --since 1d --format json -o timeline.json
```

## What it collects

| Source | Signals |
|---|---|
| Kubernetes Events | Warning/Normal events in the window: `BackOff`, `FailedScheduling`, `Unhealthy`, `OOMKilled`, `Evicted`, … |
| Pods | CrashLoopBackOff containers, last OOMKilled terminations, stuck Pending, Failed/Unknown phases |
| Deployments / ReplicaSets | Rollout progressions, new ReplicaSets per deployment revision |
| Nodes | `NotReady`, `MemoryPressure`, `DiskPressure`, `PIDPressure` |

Entries are classified `info` / `warning` / `error` and summarized (counts by level, top reasons, top objects) before the timeline.

## Pairs with postmortem-copilot

`--pmc` emits [postmortem-copilot](https://github.com/agentbantu-wq/postmortem-copilot)-compatible JSON, so the reconstructed timeline feeds straight into blameless-postmortem generation:

```bash
k8s-timeline build -n prod --since 1h --pmc -o timeline.json
pmc generate timeline.json --api-key $OPENAI_API_KEY
```

## Authentication

Uses `$KUBECONFIG`, `~/.kube/config`, or in-cluster ServiceAccount credentials. Supported auth: bearer tokens, client certificates, CA bundles. `exec` plugins and cloud `auth-provider`s are rejected with a clear error — mint a static ServiceAccount token instead:

```bash
kubectl create token deployer --duration 24h -n prod
```

Minimal RBAC needed: `get`/`list` on `events`, `pods`, `deployments`, `replicasets`, `nodes`.

## Development

```bash
python -m pytest
```
