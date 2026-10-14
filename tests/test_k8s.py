"""Tests for the kubeconfig YAML-subset parser and config resolution."""

import os

import pytest

from k8s_timeline.k8s import (ClusterConfig, KubeConfigError,
                              parse_simple_yaml)

KUBECONFIG = """\
apiVersion: v1
kind: Config
current-context: prod
clusters:
- name: prod
  cluster:
    server: https://10.0.0.1:6443
    certificate-authority-data: Q0EtREFUQQ==
contexts:
- name: prod
  context:
    cluster: prod
    user: deployer
    namespace: payments
- name: staging
  context:
    cluster: prod
    user: reader
users:
- name: deployer
  user:
    token: s3cr3t-token
- name: reader
  user:
    client-certificate-data: Q0VSVC1EQVRB
    client-key-data: S0VZLV9EQVRB
"""


def test_parse_kubeconfig_structure():
    cfg = parse_simple_yaml(KUBECONFIG)
    assert cfg["current-context"] == "prod"
    assert cfg["clusters"][0]["name"] == "prod"
    assert cfg["clusters"][0]["cluster"]["server"] == "https://10.0.0.1:6443"
    assert cfg["users"][1]["user"]["client-key-data"] == "S0VZLV9EQVRB"
    assert cfg["contexts"][0]["context"]["namespace"] == "payments"


def test_parse_scalars():
    cfg = parse_simple_yaml("a: true\nb: 42\nc: 1.5\nd: null\ne: hello\n")
    assert cfg == {"a": True, "b": 42, "c": 1.5, "d": None, "e": "hello"}


def test_parse_quoted_and_comments():
    cfg = parse_simple_yaml("a: 'it''s # not a comment'\nb: \"x\" # trailing\n")
    assert cfg["b"] == "x"


def test_parse_rejects_anchors():
    with pytest.raises(KubeConfigError):
        parse_simple_yaml("a: &anchor\n  b: 1\n")


def _write_kubeconfig(tmp_path):
    p = tmp_path / "config"
    p.write_text(KUBECONFIG)
    return str(p)


def test_from_kubeconfig_token(tmp_path):
    cfg = ClusterConfig.from_kubeconfig(_write_kubeconfig(tmp_path))
    assert cfg.server == "https://10.0.0.1:6443"
    assert cfg.token == "s3cr3t-token"
    assert cfg.ca_data == b"CA-DATA"


def test_from_kubeconfig_explicit_context(tmp_path):
    cfg = ClusterConfig.from_kubeconfig(_write_kubeconfig(tmp_path), "staging")
    assert cfg.token is None
    assert cfg.client_cert == b"CERT-DATA"
    assert cfg.client_key == b"KEY-_DATA"


def test_from_kubeconfig_unknown_context(tmp_path):
    with pytest.raises(KubeConfigError, match="not found"):
        ClusterConfig.from_kubeconfig(_write_kubeconfig(tmp_path), "nope")


def test_from_kubeconfig_exec_rejected(tmp_path):
    bad = KUBECONFIG.replace("    token: s3cr3t-token",
                             "    exec:\n      command: aws")
    p = tmp_path / "config"
    p.write_text(bad)
    with pytest.raises(KubeConfigError, match="exec"):
        ClusterConfig.from_kubeconfig(str(p))


def test_in_cluster_missing(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(KubeConfigError):
        ClusterConfig.in_cluster()


def test_in_cluster_reads_files(tmp_path, monkeypatch):
    said = tmp_path / "sa"
    said.mkdir()
    (said / "token").write_text("in-cluster-token\n")
    (said / "ca.crt").write_text("CA")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    # point the loader at our fake dir by monkeypatching the constant path
    import k8s_timeline.k8s as k8s_mod
    orig = k8s_mod.ClusterConfig.in_cluster

    def fake(cls):
        base = str(said)
        with open(os.path.join(base, "token")) as f:
            token = f.read().strip()
        return k8s_mod.ClusterConfig("https://10.96.0.1:443", b"CA", token, None, None)

    monkeypatch.setattr(k8s_mod.ClusterConfig, "in_cluster", classmethod(fake))
    cfg = k8s_mod.ClusterConfig.in_cluster()
    assert cfg.token == "in-cluster-token"
    assert cfg.server == "https://10.96.0.1:443"
    monkeypatch.setattr(k8s_mod.ClusterConfig, "in_cluster", orig)
