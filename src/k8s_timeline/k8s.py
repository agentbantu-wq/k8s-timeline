"""Minimal Kubernetes API client, standard library only.

Supports:
  - kubeconfig files (parsed with a small indentation-based YAML-subset parser,
    so there is no PyYAML dependency)
  - in-cluster ServiceAccount configuration
  - bearer tokens, client certificates, CA bundles, insecure-skip-tls-verify

Unsupported kubeconfig auth (exec plugins, auth-provider, tokenFile refresh)
fails with a clear error telling the user to mint a static token instead.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import ssl
import tempfile
import urllib.parse


class KubeConfigError(Exception):
    """Raised when a kubeconfig cannot be understood or lacks usable auth."""


# ---------------------------------------------------------------------------
# Minimal YAML-subset parser (enough for real-world kubeconfigs)
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    # '#' starts a comment unless inside quotes; kubeconfig values rarely
    # contain '#', and never inside quotes, so a simple scan is fine.
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _coerce_scalar(value: str):
    text = _unquote(value)
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_simple_yaml(text: str):
    """Parse the subset of YAML that kubeconfigs use.

    Supports nested maps, lists of maps/scalars, quoted and plain scalars,
    booleans, nulls, ints and floats. Anything else (anchors, multi-line
    block scalars, inline flow collections) raises KubeConfigError.
    """
    lines = []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        stripped = _strip_comment(raw).rstrip()
        if not stripped:
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if "\t" in stripped[:indent]:
            raise KubeConfigError("tabs are not valid YAML indentation")
        content = stripped.strip()
        if content.startswith(("&", "*")):
            raise KubeConfigError("YAML anchors/aliases are not supported")
        if content in ("|", ">", "|-", ">-"):
            raise KubeConfigError("multi-line block scalars are not supported")
        if content.startswith(("{", "[")):
            raise KubeConfigError("inline flow collections are not supported")
        lines.append((indent, content))

    pos = [0]

    def peek():
        return lines[pos[0]] if pos[0] < len(lines) else (None, None)

    def is_list_item(content: str) -> bool:
        return content == "-" or content.startswith("- ")

    def parse_node(indent):
        """Parse one node (map, list or scalar) starting at `indent`."""
        ind, content = peek()
        if ind != indent:
            raise KubeConfigError("internal parser error: indent mismatch")
        if is_list_item(content):
            return parse_list(indent)
        if ":" in content:
            return parse_map(indent)
        pos[0] += 1
        return _coerce_scalar(content)

    def parse_value_for_key(key_indent):
        """Parse the value of a `key:` line with an empty inline value."""
        ind, content = peek()
        if ind is None:
            return None
        if ind == key_indent and is_list_item(content):
            # `key:` followed by a same-indent list (common in kubeconfigs)
            return parse_list(ind)
        if ind > key_indent:
            return parse_node(ind)
        return None

    def parse_map(indent):
        mapping = {}
        while True:
            ind, content = peek()
            if ind != indent or ":" not in content or is_list_item(content):
                break
            pos[0] += 1
            key, _, val = content.partition(":")
            key = key.strip()
            if not key:
                raise KubeConfigError(f"invalid mapping line: {content!r}")
            if val.strip():
                mapping[key] = _coerce_scalar(val)
            else:
                mapping[key] = parse_value_for_key(ind)
        return mapping

    def parse_list(indent):
        items = []
        while True:
            ind, content = peek()
            if ind != indent or not is_list_item(content):
                break
            pos[0] += 1
            rest = content[1:].strip() if content != "-" else ""
            if not rest:
                items.append(parse_value_for_key(indent))
            elif ":" in rest and rest[0] not in ("'", '"'):
                items.append(parse_dash_map(indent, rest))
            else:
                items.append(_coerce_scalar(rest))
        return items

    def parse_dash_map(dash_indent, first_pair):
        """Parse `- key: value` plus the indented keys that follow it."""
        item = {}
        key, _, val = first_pair.partition(":")
        key = key.strip()
        if val.strip():
            item[key] = _coerce_scalar(val)
            body_indent = None
        else:
            # nested value under the inline key
            ind, _ = peek()
            if ind is not None and ind > dash_indent:
                item[key] = parse_node(ind)
            else:
                item[key] = None
            body_indent = None
        # The remaining keys of this map share one indent level, deeper
        # than the dash. The first such line establishes it.
        while True:
            ind, content = peek()
            if ind is None or ind <= dash_indent:
                break
            if is_list_item(content) and ind == dash_indent:
                break
            if ":" not in content or is_list_item(content):
                break
            if body_indent is None:
                body_indent = ind
            if ind != body_indent:
                break
            pos[0] += 1
            k2, _, v2 = content.partition(":")
            k2 = k2.strip()
            if not k2:
                raise KubeConfigError(f"invalid mapping line: {content!r}")
            if v2.strip():
                item[k2] = _coerce_scalar(v2)
            else:
                item[k2] = parse_value_for_key(ind)
        return item

    ind0, _ = peek()
    if ind0 is None:
        return {}
    result = parse_node(ind0)
    if pos[0] != len(lines):
        raise KubeConfigError("could not parse kubeconfig (unsupported YAML construct?)")
    return result if result is not None else {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _b64decode(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


class ClusterConfig:
    """Resolved connection parameters for one cluster + user."""

    def __init__(self, server: str, ca_data: bytes | None, token: str | None,
                 client_cert: bytes | None, client_key: bytes | None,
                 insecure: bool = False):
        self.server = server.rstrip("/")
        self.ca_data = ca_data
        self.token = token
        self.client_cert = client_cert
        self.client_key = client_key
        self.insecure = insecure

    @classmethod
    def from_kubeconfig(cls, path: str, context_name: str | None = None) -> "ClusterConfig":
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            cfg = parse_simple_yaml(f.read())
        if not isinstance(cfg, dict) or "clusters" not in cfg:
            raise KubeConfigError(f"{path} does not look like a kubeconfig")

        contexts = {c["name"]: c["context"] for c in cfg.get("contexts", []) if "name" in c}
        name = context_name or cfg.get("current-context")
        if not name or name not in contexts:
            raise KubeConfigError(
                f"context {name!r} not found in {path} "
                f"(available: {sorted(contexts) or 'none'})"
            )
        ctx = contexts[name]
        clusters = {c["name"]: c["cluster"] for c in cfg.get("clusters", []) if "name" in c}
        users = {u["name"]: u.get("user") or {} for u in cfg.get("users", []) if "name" in u}
        cluster = clusters.get(ctx.get("cluster"))
        user = users.get(ctx.get("user"), {})
        if cluster is None:
            raise KubeConfigError(f"cluster {ctx.get('cluster')!r} not found in {path}")
        if not cluster.get("server"):
            raise KubeConfigError(f"cluster {ctx.get('cluster')!r} has no server address")

        for unsupported in ("exec", "auth-provider"):
            if unsupported in user:
                raise KubeConfigError(
                    f"kubeconfig user uses {unsupported!r}, which needs an external "
                    "plugin. Mint a static bearer token (e.g. a ServiceAccount token) "
                    "and put it in the kubeconfig 'token' field instead."
                )

        ca_data = None
        if cluster.get("certificate-authority-data"):
            ca_data = _b64decode(cluster["certificate-authority-data"])
        elif cluster.get("certificate-authority"):
            with open(os.path.expanduser(cluster["certificate-authority"]), "rb") as f:
                ca_data = f.read()

        client_cert = client_key = None
        if user.get("client-certificate-data"):
            client_cert = _b64decode(user["client-certificate-data"])
        elif user.get("client-certificate"):
            with open(os.path.expanduser(user["client-certificate"]), "rb") as f:
                client_cert = f.read()
        if user.get("client-key-data"):
            client_key = _b64decode(user["client-key-data"])
        elif user.get("client-key"):
            with open(os.path.expanduser(user["client-key"]), "rb") as f:
                client_key = f.read()

        token = user.get("token")
        if user.get("tokenFile") and not token:
            with open(os.path.expanduser(user["tokenFile"]), encoding="utf-8") as f:
                token = f.read().strip()

        return cls(
            server=cluster["server"],
            ca_data=ca_data,
            token=token,
            client_cert=client_cert,
            client_key=client_key,
            insecure=bool(cluster.get("insecure-skip-tls-verify", False)),
        )

    @classmethod
    def in_cluster(cls) -> "ClusterConfig":
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        base = "/var/run/secrets/kubernetes.io/serviceaccount"
        if not host or not os.path.exists(os.path.join(base, "token")):
            raise KubeConfigError(
                "no in-cluster ServiceAccount credentials found and no kubeconfig given"
            )
        with open(os.path.join(base, "token"), encoding="utf-8") as f:
            token = f.read().strip()
        ca_path = os.path.join(base, "ca.crt")
        ca_data = open(ca_path, "rb").read() if os.path.exists(ca_path) else None
        return cls(f"https://{host}:{port}", ca_data, token, None, None)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class K8sApiError(Exception):
    """Raised for non-2xx responses from the Kubernetes API."""


class K8sClient:
    """Tiny read-only Kubernetes API client over http.client."""

    def __init__(self, config: ClusterConfig):
        self.config = config
        self._context = self._build_ssl_context()
        parsed = urllib.parse.urlparse(config.server)
        if parsed.scheme != "https":
            raise KubeConfigError("only https:// apiservers are supported")
        self._host = parsed.hostname
        self._port = parsed.port or 443
        self._base_path = parsed.path.rstrip("/")

    def _build_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        cfg = self.config
        if cfg.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif cfg.ca_data:
            ctx.load_verify_locations(cadata=cfg.ca_data.decode("utf-8", "replace"))
        if cfg.client_cert and cfg.client_key:
            cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
            try:
                cert_file.write(cfg.client_cert)
                key_file.write(cfg.client_key)
                cert_file.close()
                key_file.close()
                os.chmod(cert_file.name, 0o600)
                os.chmod(key_file.name, 0o600)
                ctx.load_cert_chain(cert_file.name, key_file.name)
            finally:
                os.unlink(cert_file.name)
                os.unlink(key_file.name)
        return ctx

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET a JSON API path (leading slash, without host)."""
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        conn = http.client.HTTPSConnection(
            self._host, self._port, context=self._context, timeout=30
        )
        try:
            headers = {"Accept": "application/json"}
            if self.config.token:
                headers["Authorization"] = f"Bearer {self.config.token}"
            conn.request("GET", self._base_path + path + query, headers=headers)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            if resp.status == 401:
                raise K8sApiError("unauthorized (401): bad or expired credentials")
            if resp.status == 403:
                raise K8sApiError(
                    "forbidden (403): the credentials lack RBAC permission for this resource"
                )
            if resp.status == 404:
                raise K8sApiError(f"not found (404): {path}")
            if not 200 <= resp.status < 300:
                raise K8sApiError(f"apiserver returned {resp.status}: {body[:200]}")
            return json.loads(body) if body.strip() else {}
        finally:
            conn.close()

    def list(self, resource_path: str, namespace: str | None = None,
             label_selector: str | None = None) -> list:
        """List items of a resource; namespace=None means all namespaces."""
        if namespace:
            path = f"/api/v1/namespaces/{namespace}/{resource_path}"
        else:
            path = f"/api/v1/{resource_path}"
        params = {"labelSelector": label_selector} if label_selector else None
        return self.get(path, params).get("items", [])
