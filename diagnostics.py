"""
Diagnostics — collect evidence, match known signatures, then (optionally) ask
an LLM to explain what deterministic rules couldn't.

The ordering is deliberate:
  1. collect_evidence()  — read-only kubectl/helm, returns structured data
  2. match_signatures()  — rule pass over that evidence, catches the common cases
  3. llm_explain()       — only the structured evidence goes to the LLM, never
                           raw credentials or the ability to run commands

The LLM never touches the cluster. It reasons over JSON we already gathered, so
the blast radius of a bad model response is a wrong explanation, never a wrong
action. Read-only by construction.
"""

import json
import subprocess
from dataclasses import dataclass, field, asdict

import credentials as creds
from config import APPS, USE_MOCK

COLLECT_TIMEOUT = 20


# --------------------------------------------------------------- data shapes

@dataclass
class Finding:
    """One diagnosed problem: what, why, and what a human might do about it."""
    signature: str            # machine key, e.g. "ImagePullBackOff"
    severity: str             # critical | warning | info
    title: str
    evidence: str             # the concrete line(s) that prove it
    suggested_fix: str        # human acts on this — never auto-applied
    kind: str = "k8s"         # k8s | helm | secret | config


@dataclass
class Evidence:
    app_name: str
    namespace: str
    cluster: str
    deploy_tool: str = "unknown"      # helm | k8s
    pods: list = field(default_factory=list)
    events: list = field(default_factory=list)
    deployment: dict = field(default_factory=dict)
    helm_status: dict = field(default_factory=dict)
    logs_tail: str = ""
    missing_refs: list = field(default_factory=list)
    collect_errors: list = field(default_factory=list)


# ------------------------------------------------------------------ plumbing

def _run(app_name, tool, args, timeout=COLLECT_TIMEOUT):
    meta = APPS[app_name]
    ctx = creds.context_for(meta["cluster"], meta["cluster"])
    if tool == "kubectl":
        cmd = ["kubectl", "--context", ctx, *args]
    else:
        cmd = ["helm", "--kube-context", ctx, "-n", meta["namespace"], *args]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=creds.kubectl_env(meta["cluster"]),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"{tool} not found on PATH"


# ------------------------------------------------------------- collect (live)

def collect_evidence(app_name: str) -> Evidence:
    if USE_MOCK:
        return _mock_evidence(app_name)

    meta = APPS[app_name]
    ns = meta["namespace"]
    ev = Evidence(app_name=app_name, namespace=ns, cluster=meta["cluster"])

    # pods (JSON so we can inspect container statuses precisely)
    rc, out, err = _run(app_name, "kubectl",
                         ["get", "pods", "-n", ns, "-l", f"app={app_name}", "-o", "json"])
    if rc == 0:
        try:
            ev.pods = _parse_pods(json.loads(out))
        except json.JSONDecodeError:
            ev.collect_errors.append("could not parse pod JSON")
    else:
        ev.collect_errors.append(f"get pods: {err.strip()}")

    # events (recent, namespace-wide — pod events show here)
    rc, out, _ = _run(app_name, "kubectl",
                      ["get", "events", "-n", ns, "--sort-by", ".lastTimestamp",
                       "-o", "json"])
    if rc == 0:
        try:
            items = json.loads(out).get("items", [])
            ev.events = [
                {"type": e.get("type"), "reason": e.get("reason"),
                 "message": e.get("message", "").strip(),
                 "object": e.get("involvedObject", {}).get("name", "")}
                for e in items[-25:]
            ]
        except json.JSONDecodeError:
            pass

    # deployment status
    rc, out, _ = _run(app_name, "kubectl",
                      ["get", "deployment", app_name, "-n", ns, "-o", "json"])
    if rc == 0:
        try:
            d = json.loads(out)
            status = d.get("status", {})
            ev.deployment = {
                "desired": d.get("spec", {}).get("replicas"),
                "ready": status.get("readyReplicas", 0),
                "available": status.get("availableReplicas", 0),
                "conditions": [
                    {"type": c.get("type"), "status": c.get("status"),
                     "reason": c.get("reason"), "message": c.get("message")}
                    for c in status.get("conditions", [])
                ],
            }
        except json.JSONDecodeError:
            pass

    # helm status (detects helm-managed apps + failed hooks/releases)
    rc, out, _ = _run(app_name, "helm", ["status", app_name, "-o", "json"])
    if rc == 0:
        ev.deploy_tool = "helm"
        try:
            h = json.loads(out)
            ev.helm_status = {
                "status": h.get("info", {}).get("status"),
                "revision": h.get("version"),
                "description": h.get("info", {}).get("description", ""),
            }
        except json.JSONDecodeError:
            pass
    else:
        ev.deploy_tool = "k8s"

    # logs from the first non-ready pod (where the real error usually is)
    target = next((p["name"] for p in ev.pods if not p["ready"]),
                  ev.pods[0]["name"] if ev.pods else None)
    if target:
        rc, out, _ = _run(app_name, "kubectl",
                          ["logs", target, "-n", ns, "--tail", "50",
                           "--all-containers", "--previous=false"])
        ev.logs_tail = out[-4000:] if rc == 0 else ""

    ev.missing_refs = _check_missing_refs(app_name, ns)
    return ev


def _parse_pods(obj):
    pods = []
    for p in obj.get("items", []):
        name = p.get("metadata", {}).get("name", "")
        phase = p.get("status", {}).get("phase", "")
        cstatuses = p.get("status", {}).get("containerStatuses", []) or []
        waiting, terminated, restarts, ready = [], [], 0, True
        for c in cstatuses:
            restarts += c.get("restartCount", 0)
            if not c.get("ready", False):
                ready = False
            st = c.get("state", {})
            if "waiting" in st:
                waiting.append(st["waiting"].get("reason", ""))
            if "terminated" in st:
                terminated.append(st["terminated"].get("reason", ""))
        pods.append({
            "name": name, "phase": phase, "ready": ready,
            "restarts": restarts, "waiting": waiting, "terminated": terminated,
        })
    return pods


def _check_missing_refs(app_name, ns):
    """Referenced secrets/configmaps that don't exist — a top deploy-failure cause."""
    missing = []
    rc, out, _ = _run(app_name, "kubectl",
                      ["get", "deployment", app_name, "-n", ns, "-o", "json"])
    if rc != 0:
        return missing
    try:
        spec = json.loads(out)["spec"]["template"]["spec"]
    except (json.JSONDecodeError, KeyError):
        return missing

    refs = {"secret": set(), "configmap": set()}
    for container in spec.get("containers", []):
        for e in container.get("envFrom", []):
            if "secretRef" in e:
                refs["secret"].add(e["secretRef"]["name"])
            if "configMapRef" in e:
                refs["configmap"].add(e["configMapRef"]["name"])
        for e in container.get("env", []):
            vf = e.get("valueFrom", {})
            if "secretKeyRef" in vf:
                refs["secret"].add(vf["secretKeyRef"]["name"])
            if "configMapKeyRef" in vf:
                refs["configmap"].add(vf["configMapKeyRef"]["name"])
    for vol in spec.get("volumes", []):
        if "secret" in vol:
            refs["secret"].add(vol["secret"]["secretName"])
        if "configMap" in vol:
            refs["configmap"].add(vol["configMap"]["name"])

    for kind, names in refs.items():
        for n in names:
            rc, _, _ = _run(app_name, "kubectl", ["get", kind, n, "-n", ns])
            if rc != 0:
                missing.append({"kind": kind, "name": n})
    return missing


# ---------------------------------------------------- signature rules (no LLM)

def match_signatures(ev: Evidence) -> list[Finding]:
    """Deterministic pass. Catches the common, unambiguous failures instantly."""
    findings = []

    for ref in ev.missing_refs:
        findings.append(Finding(
            signature="MissingReference",
            severity="critical",
            title=f"Missing {ref['kind']}: {ref['name']}",
            evidence=f"Deployment references {ref['kind']}/{ref['name']}, which does not exist in {ev.namespace}.",
            suggested_fix=f"Create {ref['kind']}/{ref['name']} in {ev.namespace}, or fix the reference in the manifest/values. "
                          + ("Use the Actions page to set the secret." if ref["kind"] == "secret" else ""),
            kind="secret" if ref["kind"] == "secret" else "config",
        ))

    for p in ev.pods:
        for reason in p["waiting"]:
            if reason in ("ImagePullBackOff", "ErrImagePull"):
                findings.append(Finding(
                    "ImagePullBackOff", "critical",
                    f"Image pull failing on {p['name']}",
                    f"Container waiting: {reason}.",
                    "Check the image tag exists in the registry and the imagePullSecret is present and valid. "
                    "A wrong VERSION/tag in the deploy params is the usual cause.",
                ))
            elif reason == "CreateContainerConfigError":
                findings.append(Finding(
                    "CreateContainerConfigError", "critical",
                    f"Container config error on {p['name']}",
                    f"Container waiting: {reason} — usually a missing secret/configmap key.",
                    "Verify every env var key referenced actually exists in the secret/configmap.",
                    kind="config",
                ))
            elif reason == "CrashLoopBackOff":
                findings.append(Finding(
                    "CrashLoopBackOff", "critical",
                    f"CrashLoopBackOff on {p['name']}",
                    f"Container restarting repeatedly ({p['restarts']} restarts). See logs below.",
                    "The app starts then exits. Check the log tail for the startup exception — "
                    "commonly a bad config value, unreachable dependency, or failed migration.",
                ))
        for reason in p["terminated"]:
            if reason == "OOMKilled":
                findings.append(Finding(
                    "OOMKilled", "critical",
                    f"Out of memory on {p['name']}",
                    "Container was OOMKilled — it exceeded its memory limit.",
                    "Raise the memory limit in resources, or investigate the leak. "
                    "Check whether load or a recent change increased footprint.",
                ))

    for e in ev.events:
        msg = e.get("message", "")
        if e.get("reason") == "FailedScheduling":
            findings.append(Finding(
                "FailedScheduling", "critical",
                "Pod cannot be scheduled",
                msg,
                "No node satisfies the pod's requirements — check resource requests, "
                "node selectors/affinity, taints, and cluster capacity.",
            ))
        elif e.get("reason") == "Unhealthy" and "readiness" in msg.lower():
            findings.append(Finding(
                "ReadinessProbeFailed", "warning",
                "Readiness probe failing",
                msg,
                "Pod is up but not passing readiness — check the probe path/port and "
                "whether the app is actually listening yet (startup time vs probe delay).",
            ))

    if ev.helm_status.get("status") in ("failed", "pending-install", "pending-upgrade"):
        findings.append(Finding(
            "HelmReleaseFailed", "critical",
            f"Helm release {ev.helm_status['status']}",
            ev.helm_status.get("description", "") or f"Release is in state {ev.helm_status['status']}.",
            "A failed helm upgrade often leaves the release stuck. Review `helm history`, "
            "then roll back to the last good revision from the Actions page.",
            kind="helm",
        ))

    d = ev.deployment
    if d and d.get("desired") and d.get("ready", 0) < d["desired"]:
        if not any(f.severity == "critical" for f in findings):
            findings.append(Finding(
                "UnderReplicated", "warning",
                f"Only {d.get('ready', 0)}/{d['desired']} replicas ready",
                "Deployment has not reached desired replica count.",
                "Some pods aren't ready — inspect individual pod status and events above.",
            ))

    return findings


# ------------------------------------------------------------- LLM reasoning

def build_llm_prompt(ev: Evidence, findings: list[Finding]) -> str:
    """
    The exact text handed to the model. Only structured evidence — no
    credentials, no tools. Kept as a function so it's easy to inspect/log.
    """
    payload = {
        "app": ev.app_name,
        "namespace": ev.namespace,
        "deploy_tool": ev.deploy_tool,
        "deployment": ev.deployment,
        "pods": ev.pods,
        "recent_events": ev.events[-12:],
        "helm_status": ev.helm_status,
        "missing_references": ev.missing_refs,
        "log_tail": ev.logs_tail[-2000:],
        "rule_based_findings": [asdict(f) for f in findings],
    }
    return (
        "You are a Kubernetes/Helm deployment troubleshooting assistant.\n"
        "Below is structured, read-only evidence collected from a failed or "
        "unhealthy application. Rule-based checks have already run and are "
        "included as `rule_based_findings`.\n\n"
        "Your job:\n"
        "1. State the single most likely root cause in one or two sentences.\n"
        "2. If the rules missed something in the logs/events, add it.\n"
        "3. Give concrete, ordered remediation steps a human can take. Do NOT "
        "assume anything not present in the evidence. If evidence is "
        "insufficient, say what to collect next.\n"
        "Keep it tight and operational. Never invent resource names.\n\n"
        f"EVIDENCE:\n{json.dumps(payload, indent=2)}"
    )


def llm_explain(ev: Evidence, findings: list[Finding]) -> dict:
    """
    Ask the configured LLM to explain. Returns {ok, text, error}.
    Read-only: the model gets evidence text and returns prose. Nothing it says
    is executed.
    """
    from llm_client import complete
    prompt = build_llm_prompt(ev, findings)
    return complete(prompt)


# ------------------------------------------------------------------ full run

def diagnose(app_name: str, use_llm: bool = True) -> dict:
    ev = collect_evidence(app_name)
    findings = match_signatures(ev)
    result = {
        "app": app_name,
        "evidence": asdict(ev),
        "findings": [asdict(f) for f in findings],
        "llm": None,
    }
    if use_llm:
        result["llm"] = llm_explain(ev, findings)
    return result


# ------------------------------------------------------------------ mock data

def _mock_evidence(app_name: str) -> Evidence:
    """Deterministic per-app failure scenarios so the UI demos every signature."""
    ns = APPS[app_name]["namespace"]
    scenarios = {
        "api-service": Evidence(
            app_name, ns, APPS[app_name]["cluster"], deploy_tool="helm",
            pods=[{"name": f"{app_name}-7d4b8c9f5-2xk9p", "phase": "Pending", "ready": False,
                   "restarts": 0, "waiting": ["ImagePullBackOff"], "terminated": []}],
            events=[{"type": "Warning", "reason": "Failed",
                     "message": 'Failed to pull image "registry.example.com/api-service:26.07.9": not found',
                     "object": f"{app_name}-7d4b8c9f5-2xk9p"}],
            deployment={"desired": 3, "ready": 0, "available": 0, "conditions": []},
            helm_status={"status": "deployed", "revision": 42, "description": "Upgrade complete"},
            logs_tail="",
        ),
        "ui-app": Evidence(
            app_name, ns, APPS[app_name]["cluster"], deploy_tool="helm",
            pods=[{"name": f"{app_name}-abc-1", "phase": "Running", "ready": False,
                   "restarts": 7, "waiting": ["CrashLoopBackOff"], "terminated": []}],
            events=[{"type": "Warning", "reason": "BackOff",
                     "message": "Back-off restarting failed container",
                     "object": f"{app_name}-abc-1"}],
            deployment={"desired": 2, "ready": 0, "available": 0, "conditions": []},
            helm_status={"status": "deployed", "revision": 18, "description": "Upgrade complete"},
            logs_tail=("Starting ui-app v26.07...\n"
                       "FATAL: config key API_BASE_URL is empty\n"
                       "Error: cannot start without upstream URL\n"
                       "process exited with code 1"),
        ),
        "batch-reconciler": Evidence(
            app_name, ns, APPS[app_name]["cluster"], deploy_tool="helm",
            pods=[{"name": f"{app_name}-xyz-0", "phase": "Pending", "ready": False,
                   "restarts": 0, "waiting": ["CreateContainerConfigError"], "terminated": []}],
            events=[{"type": "Warning", "reason": "Failed",
                     "message": 'secret "batch-reconciler-db" not found',
                     "object": f"{app_name}-xyz-0"}],
            deployment={"desired": 1, "ready": 0, "available": 0, "conditions": []},
            helm_status={"status": "failed", "revision": 23,
                         "description": "Upgrade failed: post-upgrade hooks failed"},
            missing_refs=[{"kind": "secret", "name": "batch-reconciler-db"}],
            logs_tail="",
        ),
    }
    return scenarios.get(app_name, Evidence(
        app_name, ns, APPS[app_name]["cluster"], deploy_tool="helm",
        pods=[{"name": f"{app_name}-ok-1", "phase": "Running", "ready": True,
               "restarts": 0, "waiting": [], "terminated": []}],
        deployment={"desired": 3, "ready": 3, "available": 3, "conditions": []},
        helm_status={"status": "deployed", "revision": 12, "description": "Install complete"},
        logs_tail="健康 all systems nominal",
    ))
