"""
Write actions — scale, update secret, helm rollback.

Every action is a declarative entry in ACTIONS: a JSON-schema-ish param spec
plus an executor. The UI renders forms from the spec; nothing is hardcoded per
action. That same spec is what you'd hand an LLM as a tool definition later —
see action_schemas() at the bottom.

Design rules that matter here:
  - No shell. subprocess gets a list; user strings never reach a shell.
  - Secret values never touch argv (visible in `ps`) or disk. The secret path
    reads the existing object, merges in memory, and pipes YAML over stdin.
  - Every attempt is audited before and after, including failures.
"""

import base64
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import credentials as creds
from config import APPS, USE_MOCK

KUBECTL_TIMEOUT = 30
HELM_TIMEOUT = 120


@dataclass
class Result:
    ok: bool
    summary: str
    detail: str = ""
    command: str = ""


# ------------------------------------------------------------------- plumbing

def _kubectl(app_name: str, args: list, stdin: str | None = None, timeout=KUBECTL_TIMEOUT):
    meta = APPS[app_name]
    ctx = creds.context_for(meta["cluster"], meta["cluster"])
    cmd = ["kubectl", "--context", ctx, *args]
    display = " ".join(cmd)

    if USE_MOCK:
        return display, 0, f"(mock) would run: {display}", ""

    try:
        r = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=creds.kubectl_env(meta["cluster"]),
        )
        return display, r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return display, 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return display, 127, "", "kubectl not found on PATH"


def _helm(app_name: str, args: list, timeout=HELM_TIMEOUT):
    meta = APPS[app_name]
    ctx = creds.context_for(meta["cluster"], meta["cluster"])
    cmd = ["helm", "--kube-context", ctx, "-n", meta["namespace"], *args]
    display = " ".join(cmd)

    if USE_MOCK:
        return display, 0, f"(mock) would run: {display}", ""

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=creds.kubectl_env(meta["cluster"]),
        )
        return display, r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return display, 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return display, 127, "", "helm not found on PATH"


# -------------------------------------------------------------------- readers

def current_replicas(app_name: str):
    """Current replica count, or None if it can't be read."""
    if USE_MOCK:
        return 3
    meta = APPS[app_name]
    _, rc, out, _ = _kubectl(
        app_name,
        ["get", "deployment", app_name, "-n", meta["namespace"],
         "-o", "jsonpath={.spec.replicas}"],
    )
    try:
        return int(out.strip()) if rc == 0 else None
    except ValueError:
        return None


def list_secrets(app_name: str):
    """Secret names in the app's namespace (names only — never values)."""
    if USE_MOCK:
        return [f"{app_name}-config", f"{app_name}-tls", "registry-pull"]
    meta = APPS[app_name]
    _, rc, out, _ = _kubectl(
        app_name,
        ["get", "secrets", "-n", meta["namespace"],
         "-o", "jsonpath={.items[*].metadata.name}"],
    )
    return out.split() if rc == 0 else []


def secret_keys(app_name: str, secret_name: str):
    """Key names inside a secret. Values are never returned."""
    if USE_MOCK:
        return ["DB_PASSWORD", "API_KEY", "REDIS_URL"]
    _, rc, out, _ = _kubectl(
        app_name,
        ["get", "secret", secret_name, "-n", APPS[app_name]["namespace"],
         "-o", "jsonpath={.data}"],
    )
    if rc != 0:
        return []
    try:
        return sorted(json.loads(out).keys())
    except (json.JSONDecodeError, AttributeError):
        return []


def helm_history(app_name: str):
    """Helm revisions, newest first."""
    if USE_MOCK:
        return [
            {"revision": 42, "updated": "2026-07-17 05:12", "status": "deployed", "chart": f"{app_name}-2.4.1"},
            {"revision": 41, "updated": "2026-07-15 11:40", "status": "superseded", "chart": f"{app_name}-2.4.0"},
            {"revision": 40, "updated": "2026-07-11 09:02", "status": "superseded", "chart": f"{app_name}-2.3.8"},
        ]
    _, rc, out, _ = _helm(app_name, ["history", app_name, "-o", "json"])
    if rc != 0:
        return []
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return []
    return sorted(rows, key=lambda r: r.get("revision", 0), reverse=True)


# ------------------------------------------------------------------ executors

def do_scale(app_name: str, replicas: int, **_) -> Result:
    meta = APPS[app_name]
    cmd, rc, out, err = _kubectl(
        app_name,
        ["scale", "deployment", app_name, "-n", meta["namespace"],
         f"--replicas={int(replicas)}"],
    )
    if rc == 0:
        return Result(True, f"Scaled {app_name} to {replicas} replica(s)", out, cmd)
    return Result(False, f"Scale failed (exit {rc})", err or out, cmd)


def do_update_secret(app_name: str, secret_name: str, key: str, value: str, **_) -> Result:
    """
    Merge one key into an existing secret.

    Reads the current object, updates the key in memory, applies the whole
    manifest over stdin. The value never lands in argv or on disk.
    """
    meta = APPS[app_name]
    ns = meta["namespace"]

    if USE_MOCK:
        return Result(
            True,
            f"Updated key {key} in secret {secret_name}",
            f"(mock) secret/{secret_name} configured",
            f"kubectl apply -f - -n {ns}  # merged key {key}",
        )

    cmd, rc, out, err = _kubectl(app_name, ["get", "secret", secret_name, "-n", ns, "-o", "json"])
    if rc != 0:
        return Result(False, f"Couldn't read secret {secret_name}", err or out, cmd)

    try:
        obj = json.loads(out)
    except json.JSONDecodeError as e:
        return Result(False, "Secret returned unparseable JSON", str(e), cmd)

    obj.setdefault("data", {})[key] = base64.b64encode(value.encode()).decode()
    for drop in ("creationTimestamp", "resourceVersion", "uid", "managedFields"):
        obj.get("metadata", {}).pop(drop, None)

    apply_cmd, rc, out, err = _kubectl(
        app_name, ["apply", "-n", ns, "-f", "-"], stdin=json.dumps(obj)
    )
    if rc != 0:
        return Result(False, "Secret update failed", err or out, apply_cmd)

    return Result(
        True,
        f"Updated key {key} in secret {secret_name}",
        out + "\n\nNote: running pods keep the old value until restarted. "
              "Roll the deployment if this secret is mounted as env vars.",
        apply_cmd,
    )


def do_rollback(app_name: str, revision: int, **_) -> Result:
    cmd, rc, out, err = _helm(app_name, ["rollback", app_name, str(int(revision)), "--wait"])
    if rc == 0:
        return Result(True, f"Rolled {app_name} back to revision {revision}", out, cmd)
    return Result(False, f"Rollback failed (exit {rc})", err or out, cmd)


# ------------------------------------------------------------ action registry

ACTIONS = {
    "scale": {
        "label": "Scale replicas",
        "icon": "⇅",
        "description": "Change the replica count of a deployment.",
        "danger": "medium",
        "confirm": "checkbox",
        "params": {
            "replicas": {"type": "int", "min": 0, "max": 20, "label": "Replicas"},
        },
        "run": do_scale,
    },
    "update_secret": {
        "label": "Update secret",
        "icon": "🔑",
        "description": "Set one key in an existing secret. Current values are never displayed.",
        "danger": "high",
        "confirm": "checkbox",
        "params": {
            "secret_name": {"type": "select_secret", "label": "Secret"},
            "key": {"type": "select_key", "label": "Key"},
            "value": {"type": "password", "label": "New value"},
        },
        "run": do_update_secret,
    },
    "rollback": {
        "label": "Rollback release",
        "icon": "↩",
        "description": "Roll the Helm release back to an earlier revision.",
        "danger": "high",
        "confirm": "typed",
        "params": {
            "revision": {"type": "select_revision", "label": "Target revision"},
        },
        "run": do_rollback,
    },
}


def run_action(action_key: str, app_name: str, params: dict, actor: str) -> Result:
    """Execute an action and audit it — including failures and crashes."""
    from backend import record_audit

    spec = ACTIONS[action_key]
    safe_params = {
        k: ("***" if spec["params"].get(k, {}).get("type") == "password" else v)
        for k, v in params.items()
    }

    try:
        result = spec["run"](app_name=app_name, **params)
    except Exception as e:
        result = Result(False, "Action raised an exception", str(e))

    record_audit(
        actor=actor,
        action=action_key,
        app_name=app_name,
        params=json.dumps(safe_params),
        ok=result.ok,
        detail=result.summary,
    )
    return result


def action_schemas():
    """
    Tool definitions for an LLM, derived from ACTIONS.

    Lets a future agent call the same audited, allowlisted executors the UI
    uses — rather than being handed a shell.
    """
    return [
        {
            "name": key,
            "description": spec["description"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "enum": list(APPS.keys())},
                    **{
                        pname: {"type": "integer" if p["type"] == "int" else "string",
                                "description": p["label"]}
                        for pname, p in spec["params"].items()
                    },
                },
                "required": ["app_name", *spec["params"].keys()],
            },
        }
        for key, spec in ACTIONS.items()
    ]
