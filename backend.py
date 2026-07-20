"""
Backend — data access and the two integration points.

Everything the UI needs goes through here, so app.py never knows whether it's
talking to mock data or your real GitLab/kubectl. Deployment history is kept in
a local SQLite file so counts survive restarts.
"""

import random
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import streamlit as st

from config import (
    APPS, USE_MOCK, GITLAB_URL, GITLAB_TOKEN, KUBECTL_ACTIONS,
)

DB_PATH = Path(__file__).parent / "deployments.db"


# ------------------------------------------------------------------ database

def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS deployments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                deploy_id  TEXT,
                app_name   TEXT NOT NULL,
                status     TEXT NOT NULL,
                ci         TEXT,
                ref_id     TEXT,
                web_url    TEXT,
                cluster    TEXT,
                namespace  TEXT,
                deployed_at TEXT NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_app ON deployments(app_name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deploy_id ON deployments(deploy_id)")
        # Migrate older DBs that predate the new columns.
        cols = {r[1] for r in c.execute("PRAGMA table_info(deployments)").fetchall()}
        for col in ("deploy_id", "ci", "ref_id", "web_url", "cluster", "namespace"):
            if col not in cols:
                c.execute(f"ALTER TABLE deployments ADD COLUMN {col} TEXT")

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                actor    TEXT NOT NULL,
                action   TEXT NOT NULL,
                app_name TEXT NOT NULL,
                params   TEXT,
                ok       INTEGER NOT NULL,
                detail   TEXT
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC)")

        # Seed once so the Overview page isn't empty on a fresh clone.
        n = c.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        if n == 0 and USE_MOCK:
            _seed(c)


def _seed(c):
    now = datetime.now()
    for app_name in APPS:
        runs = random.randint(4, 14)
        for i in range(runs):
            # Weight toward success so the rate lands in a realistic band.
            status = "Success" if random.random() < 0.83 else "Failed"
            ts = now - timedelta(hours=random.randint(1, 500))
            c.execute(
                "INSERT INTO deployments (app_name, status, deployed_at) VALUES (?,?,?)",
                (app_name, status, ts.isoformat(timespec="seconds")),
            )


def record_audit(actor, action, app_name, params, ok, detail):
    """Append-only log of every write action attempted."""
    with _conn() as c:
        c.execute(
            "INSERT INTO audit (ts, actor, action, app_name, params, ok, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), actor, action,
             app_name, params, 1 if ok else 0, detail),
        )
    get_audit_log.clear()


@st.cache_data(ttl=10)
def get_audit_log(limit: int = 50):
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, actor, action, app_name, params, ok, detail "
            "FROM audit ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"when": r[0].replace("T", " "), "actor": r[1], "action": r[2],
         "app": r[3], "params": r[4], "result": "OK" if r[5] else "FAILED",
         "detail": r[6]}
        for r in rows
    ]


def current_actor() -> str:
    """
    Who is performing this action.

    Reads the identity header your reverse proxy sets after SSO. With a shared
    service-account token, this is the ONLY thing distinguishing one operator
    from another in the audit log — without it, every row reads 'unknown'.
    """
    try:
        headers = st.context.headers or {}
    except Exception:
        return "unknown"

    for h in ("X-Forwarded-User", "X-Auth-Request-Email", "X-Forwarded-Email",
              "X-Forwarded-Preferred-Username", "Remote-User"):
        val = headers.get(h) or headers.get(h.lower())
        if val:
            return val
    return "unknown"


def record_deployment(app_name: str, status: str, deploy_id: str = None,
                      ci: str = None, ref_id: str = None, web_url: str = None):
    """
    Log a deployment outcome. Optionally records the dashboard deploy_id and CI
    details so a failure can later be diagnosed by pasting that ID.
    Clears the stats cache so Overview updates.
    """
    from config import APPS
    meta = APPS.get(app_name, {})
    with _conn() as c:
        c.execute(
            "INSERT INTO deployments "
            "(deploy_id, app_name, status, ci, ref_id, web_url, cluster, namespace, deployed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (deploy_id, app_name, status, ci, ref_id, web_url,
             meta.get("cluster"), meta.get("namespace"),
             datetime.now().isoformat(timespec="seconds")),
        )
    get_app_stats.clear()


def lookup_deployment(deploy_id: str) -> dict | None:
    """Resolve a dashboard deploy ID to its app, CI, pipeline ref, and cluster."""
    with _conn() as c:
        r = c.execute(
            "SELECT deploy_id, app_name, status, ci, ref_id, web_url, cluster, namespace, deployed_at "
            "FROM deployments WHERE deploy_id=? ORDER BY id DESC LIMIT 1",
            (deploy_id,),
        ).fetchone()
    if not r:
        return None
    keys = ["deploy_id", "app_name", "status", "ci", "ref_id",
            "web_url", "cluster", "namespace", "deployed_at"]
    return dict(zip(keys, r))


def recent_failed_deploys(limit: int = 20) -> list[dict]:
    """Recent failed deployments that carry a deploy_id — for the diagnose picker."""
    with _conn() as c:
        rows = c.execute(
            "SELECT deploy_id, app_name, ci, ref_id, deployed_at FROM deployments "
            "WHERE status='Failed' AND deploy_id IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"deploy_id": r[0], "app_name": r[1], "ci": r[2],
             "ref_id": r[3], "deployed_at": r[4]} for r in rows]


# --------------------------------------------------------------------- stats

@st.cache_data(ttl=30)
def get_app_stats():
    """One row per app: last status, last deploy time, success/failure counts."""
    rows = []
    with _conn() as c:
        for app_name, meta in APPS.items():
            counts = dict(
                c.execute(
                    "SELECT status, COUNT(*) FROM deployments WHERE app_name=? GROUP BY status",
                    (app_name,),
                ).fetchall()
            )
            last = c.execute(
                "SELECT status, deployed_at FROM deployments WHERE app_name=? "
                "ORDER BY deployed_at DESC LIMIT 1",
                (app_name,),
            ).fetchone()

            rows.append({
                "app_name": app_name,
                "last_status": last[0] if last else "Never deployed",
                "last_deployed": _humanize(last[1]) if last else "—",
                "success_count": counts.get("Success", 0),
                "failure_count": counts.get("Failed", 0),
                "rancher_url": meta["rancher_url"],
                "kibana_url": meta["kibana_url"],
            })
    return rows


def _humanize(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    delta = datetime.now() - dt
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    if delta < timedelta(days=7):
        return f"{delta.days}d ago"
    return dt.strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------- INTEGRATION POINT 1: GitLab

def trigger_pipeline(app_name: str, branch: str, params: dict) -> dict:
    """
    Trigger a deployment pipeline.

    Live mode posts to the GitLab pipeline API. Params become CI variables,
    so `.gitlab-ci.yml` reads them as $ENV, $VERSION, $SKIP_TESTS, $ROLLBACK.
    """
    meta = APPS[app_name]
    variables = {k: str(v) for k, v in params.items()}

    if USE_MOCK:
        time_id = random.randint(100000, 999999)
        ok = random.random() < 0.85
        return {
            "ok": ok,
            "pipeline_id": time_id,
            "app": app_name,
            "ref": branch,
            "project_id": meta["gitlab_project_id"],
            "variables": variables,
            "web_url": f"{GITLAB_URL}/{app_name}/-/pipelines/{time_id}",
            "status": "created" if ok else "failed",
            "error": None if ok else "mock: pipeline rejected (no runner available)",
            "triggered_at": datetime.now().isoformat(timespec="seconds"),
        }

    import requests  # imported lazily so mock mode has no hard dependency

    url = f"{GITLAB_URL}/api/v4/projects/{meta['gitlab_project_id']}/pipeline"
    payload = {
        "ref": branch,
        "variables": [{"key": k, "value": v} for k, v in variables.items()],
    }
    try:
        r = requests.post(
            url,
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "ok": True,
            "pipeline_id": data["id"],
            "app": app_name,
            "ref": branch,
            "variables": variables,
            "web_url": data.get("web_url"),
            "status": data.get("status"),
            "triggered_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        return {"ok": False, "app": app_name, "ref": branch,
                "variables": variables, "error": str(e)}


# ------------------------------------------------ INTEGRATION POINT 2: kubectl

def run_kubectl(action: str, app_name: str):
    """
    Run an allowlisted read-only kubectl command.

    Returns (displayed_command, output). Args come from the KUBECTL_ACTIONS
    template — never from free-form user input — and subprocess runs without a
    shell, so there's no injection surface here.
    """
    meta = APPS[app_name]
    template = KUBECTL_ACTIONS[action]
    args = [a.format(ns=meta["namespace"], app=app_name) for a in template]
    display = "kubectl " + " ".join(args) + f"  --context {meta['cluster']}"

    if USE_MOCK:
        return display, _mock_output(action, app_name, meta)

    cmd = ["kubectl", "--context", meta["cluster"], *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return display, (r.stdout or r.stderr or "(no output)")
    except subprocess.TimeoutExpired:
        return display, "Error: command timed out after 30s"
    except FileNotFoundError:
        return display, "Error: kubectl not found on PATH"
    except Exception as e:
        return display, f"Error: {e}"


def _mock_output(action, app, meta):
    ns = meta["namespace"]
    if action == "Pods":
        return (
            "NAME                              READY   STATUS    RESTARTS   AGE   IP           NODE\n"
            f"{app}-7d4b8c9f5-2xk9p            1/1     Running   0          4h    10.42.1.23   node-03\n"
            f"{app}-7d4b8c9f5-8mq2r            1/1     Running   0          4h    10.42.2.11   node-05\n"
            f"{app}-7d4b8c9f5-vn4tz            1/1     Running   1          4h    10.42.0.47   node-01"
        )
    if action == "Deployment":
        return (
            "NAME              READY   UP-TO-DATE   AVAILABLE   AGE   CONTAINERS   IMAGE\n"
            f"{app}   3/3     3            3           62d   {app}   registry.example.com/{app}:26.07.1"
        )
    if action == "Describe":
        return (
            f"Name:                   {app}\n"
            f"Namespace:              {ns}\n"
            "Replicas:               3 desired | 3 updated | 3 total | 3 available\n"
            "StrategyType:           RollingUpdate\n"
            "RollingUpdateStrategy:  25% max unavailable, 25% max surge\n"
            "Conditions:\n"
            "  Type           Status  Reason\n"
            "  ----           ------  ------\n"
            "  Available      True    MinimumReplicasAvailable\n"
            "  Progressing    True    NewReplicaSetAvailable"
        )
    if action == "Logs":
        return (
            '{"ts":"2026-07-17T09:14:02Z","level":"info","msg":"health check ok"}\n'
            '{"ts":"2026-07-17T09:14:07Z","level":"info","msg":"request handled","path":"/v1/accounts","status":200,"ms":42}\n'
            '{"ts":"2026-07-17T09:14:11Z","level":"warn","msg":"upstream slow","upstream":"auth-service","ms":812}\n'
            '{"ts":"2026-07-17T09:14:15Z","level":"info","msg":"request handled","path":"/v1/balance","status":200,"ms":31}'
        )
    return (
        "LAST SEEN   TYPE      REASON              OBJECT                    MESSAGE\n"
        f"4h          Normal    ScalingReplicaSet   deployment/{app}   Scaled up replica set to 3\n"
        f"4h          Normal    Pulled              pod/{app}-7d4b8c9f5-2xk9p   Container image pulled\n"
        f"4h          Normal    Started             pod/{app}-7d4b8c9f5-2xk9p   Started container"
    )
