"""
Credentials — loaded from a JSON file kept outside version control.

  export DASHBOARD_CREDENTIALS=/etc/dashboard/credentials.json
  chmod 600 /etc/dashboard/credentials.json

Falls back to credentials.json next to this file, then to env vars, then to
empty (mock mode). See credentials.example.json for the shape.
"""

import json
import os
from pathlib import Path

_DEFAULT = Path(__file__).parent / "credentials.json"
_PATH = Path(os.getenv("DASHBOARD_CREDENTIALS", _DEFAULT))

_creds = {}
if _PATH.exists():
    mode = _PATH.stat().st_mode & 0o077
    if mode:
        # World/group readable secrets are a finding waiting to happen.
        print(f"WARNING: {_PATH} is group/world readable — run: chmod 600 {_PATH}")
    _creds = json.loads(_PATH.read_text())


def get(*keys, default=None):
    """Nested lookup: get('gitlab', 'token')."""
    node = _creds
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def cluster(name: str) -> dict:
    """Per-cluster kubectl settings: context name and optional kubeconfig path."""
    return get("clusters", name, default={}) or {}


def kubectl_env(cluster_name: str) -> dict:
    """Environment for a kubectl subprocess — injects KUBECONFIG if pinned."""
    env = os.environ.copy()
    kubeconfig = cluster(cluster_name).get("kubeconfig")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig
    return env


def context_for(cluster_name: str, fallback: str) -> str:
    """kubectl --context value. Defaults to the cluster key itself."""
    return cluster(cluster_name).get("context", fallback)


def loaded() -> bool:
    return bool(_creds)


def source() -> str:
    return str(_PATH) if _PATH.exists() else "(none — using env vars / mock)"
