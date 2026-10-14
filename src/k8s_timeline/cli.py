"""Command-line interface for k8s-timeline."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import __version__
from . import format as fmt
from .collectors import (collect_deployments, collect_events, collect_nodes,
                         collect_pods)
from .k8s import ClusterConfig, K8sApiError, K8sClient, KubeConfigError
from .timeline import build_timeline, parse_since, summarize


def build_client(args: argparse.Namespace) -> K8sClient:
    kubeconfig = args.kubeconfig
    if kubeconfig is None and "KUBECONFIG" in __import__("os").environ:
        kubeconfig = __import__("os").environ["KUBECONFIG"]
    try:
        if kubeconfig:
            cfg = ClusterConfig.from_kubeconfig(kubeconfig, args.context)
        else:
            try:
                cfg = ClusterConfig.from_kubeconfig("~/.kube/config", args.context)
            except (OSError, KubeConfigError):
                cfg = ClusterConfig.in_cluster()
    except (OSError, KubeConfigError) as exc:
        print(f"Cluster config error: {exc}", file=sys.stderr)
        sys.exit(2)
    if args.insecure_skip_tls_verify:
        cfg.insecure = True
    return K8sClient(cfg)


def cmd_build(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    try:
        since = parse_since(args.since, now)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    client = build_client(args)
    ns = args.namespace

    try:
        entries = []
        entries.extend(collect_events(client, ns, since))
        entries.extend(collect_pods(client, ns, since, now))
        entries.extend(collect_deployments(client, ns, since))
        if not ns or args.include_nodes:
            entries.extend(collect_nodes(client, since, now))
    except K8sApiError as exc:
        print(f"Kubernetes API error: {exc}", file=sys.stderr)
        return 1

    entries = build_timeline(entries)
    summary = summarize(entries)

    if args.pmc:
        output = fmt.to_postmortem_copilot(entries, service=args.service,
                                           severity=args.severity)
    elif args.format == "json":
        output = fmt.to_json(entries, summary)
    elif args.format == "markdown":
        output = fmt.to_markdown(entries, summary, ns)
    else:
        output = fmt.to_text(entries, summary)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output + "\n")
        print(f"Wrote {len(entries)} timeline entries to {args.output}", file=sys.stderr)
    else:
        print(output)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Verify cluster connectivity and RBAC without collecting anything."""
    client = build_client(args)
    try:
        version = client.get("/version")
        ns = args.namespace or "default"
        try:
            client.list("events", namespace=ns)
            events_rbac = "ok"
        except K8sApiError as exc:
            events_rbac = str(exc)
        print(f"apiserver: {version.get('gitVersion', '?')} reachable")
        print(f"events in namespace '{ns}': {events_rbac}")
        return 0
    except K8sApiError as exc:
        print(f"Cluster unreachable: {exc}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="k8s-timeline",
        description="Reconstruct Kubernetes incident timelines from cluster state.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--kubeconfig", default=None,
                        help="path to kubeconfig (default: $KUBECONFIG or ~/.kube/config)")
    parser.add_argument("--context", default=None, help="kubeconfig context to use")
    parser.add_argument("--insecure-skip-tls-verify", action="store_true",
                        help="skip TLS verification (testing only)")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build an incident timeline")
    b.add_argument("-n", "--namespace", default=None,
                   help="namespace to inspect (default: all namespaces)")
    b.add_argument("--since", default="2h",
                   help="how far back to look: 30m/2h/1d/1w or RFC3339 (default: 2h)")
    b.add_argument("--include-nodes", action="store_true",
                   help="include node conditions even when a namespace is given")
    b.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    b.add_argument("--pmc", action="store_true",
                   help="emit postmortem-copilot-compatible JSON (pairs with the 'pmc' CLI)")
    b.add_argument("--service", default="",
                   help="service name for --pmc output (default: namespace or 'k8s')")
    b.add_argument("--severity", default="SEV-3", help="severity label for --pmc output")
    b.add_argument("-o", "--output", default=None, help="write to file instead of stdout")
    b.set_defaults(func=cmd_build)

    d = sub.add_parser("doctor", help="verify cluster connectivity and RBAC")
    d.add_argument("-n", "--namespace", default=None)
    d.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "build" and args.pmc and not args.service:
        args.service = args.namespace or "k8s"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
