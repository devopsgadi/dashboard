"""
Deploy-ID diagnosis — the "paste an ID, get root cause" flow.

Pipeline:
  1. resolve_deploy_id()   — dashboard deploy_id -> app, CI, pipeline ref, cluster
  2. collect_ci_evidence() — pull the GitLab job trace / Jenkins console for that run
  3. (reuse) diagnostics.collect_evidence() — pod logs, events, helm status
  4. correlate + one LLM pass over the COMBINED bundle

This is collect-then-reason: deterministic gathering, a single explanation call.
No agent loop, no fetch tools handed to the model — it reasons over evidence we
already assembled, and it can only produce text. Read-only by construction.

The key idea: the deploy_id ties the CI side (why the pipeline failed) to the
cluster side (what state the pods are in). Half of deploy failures never reach a
pod — a failed helm upgrade, a rejected manifest, a test-stage failure — so the
CI trace is often where the actual error lives.
"""

import re

import credentials as creds
from config import USE_MOCK
import diagnostics as diag
from backend import lookup_deployment

CI_LOG_TAIL = 6000        # chars of CI log to keep (the end holds the failure)
REQUEST_TIMEOUT = 25


def diagnose_by_id(deploy_id: str, use_llm: bool = True) -> dict:
    """Full flow. Returns a result dict the UI renders, or an error."""
    dep = lookup_deployment(deploy_id.strip())
    if not dep:
        return {"ok": False,
                "error": f"No deployment found for ID '{deploy_id}'. "
                         "IDs come from the Bulk/Batch deploy pages."}

    app = dep["app_name"]

    # CI side — why the pipeline itself failed
    ci_evidence = collect_ci_evidence(dep)

    # Cluster side — reuse the existing collector + signature rules
    cluster_ev = diag.collect_evidence(app)
    findings = diag.match_signatures(cluster_ev)

    # CI-specific signatures (things the cluster collector can't see)
    findings = _match_ci_signatures(ci_evidence, findings)

    result = {
        "ok": True,
        "deploy": dep,
        "ci_evidence": ci_evidence,
        "cluster_evidence": diag.asdict(cluster_ev),
        "findings": [diag.asdict(f) for f in findings],
        "llm": None,
    }

    if use_llm:
        result["llm"] = _llm_correlate(dep, ci_evidence, cluster_ev, findings)
    return result


# ------------------------------------------------------------- CI log fetching

def collect_ci_evidence(dep: dict) -> dict:
    """Pull the CI job trace for this deploy's pipeline/build."""
    ci = dep.get("ci")
    ref_id = dep.get("ref_id")

    if USE_MOCK:
        return _mock_ci_evidence(dep)

    if ci == "gitlab":
        return _gitlab_job_trace(dep)
    if ci == "jenkins":
        return _jenkins_console(dep)
    return {"ci": ci, "log_tail": "", "status": "unknown",
            "errors": [f"no CI log fetcher for ci='{ci}'"]}


def _gitlab_job_trace(dep: dict) -> dict:
    import requests
    base = creds.get("gitlab", "url", default="").rstrip("/")
    token = creds.get("gitlab", "token")
    web_url = dep.get("web_url", "")

    m = re.search(r"/projects/(\d+)", web_url) or re.search(r"/pipelines/(\d+)", web_url)
    pipeline_id = dep.get("ref_id")
    evidence = {"ci": "gitlab", "log_tail": "", "status": "", "errors": [], "web_url": web_url}

    # Resolve project from stored web_url; fall back to erroring cleanly.
    proj = re.search(r"gitlab[^/]*/(.+?)/-/", web_url)
    if not (base and token and pipeline_id):
        evidence["errors"].append("missing gitlab base/token/pipeline id")
        return evidence

    try:
        # find the failed job in the pipeline, then fetch its trace
        proj_enc = _project_id_from_url(web_url)
        jobs = requests.get(
            f"{base}/api/v4/projects/{proj_enc}/pipelines/{pipeline_id}/jobs",
            headers={"PRIVATE-TOKEN": token}, timeout=REQUEST_TIMEOUT,
        ).json()
        failed = next((j for j in jobs if j.get("status") == "failed"), jobs[0] if jobs else None)
        if not failed:
            evidence["errors"].append("no jobs found for pipeline")
            return evidence
        evidence["status"] = failed.get("status")
        evidence["job_name"] = failed.get("name")
        trace = requests.get(
            f"{base}/api/v4/projects/{proj_enc}/jobs/{failed['id']}/trace",
            headers={"PRIVATE-TOKEN": token}, timeout=REQUEST_TIMEOUT,
        ).text
        evidence["log_tail"] = trace[-CI_LOG_TAIL:]
    except Exception as e:
        evidence["errors"].append(str(e))
    return evidence


def _jenkins_console(dep: dict) -> dict:
    import requests
    user = creds.get("jenkins", "user")
    token = creds.get("jenkins", "api_token")
    web_url = dep.get("web_url", "").rstrip("/")
    evidence = {"ci": "jenkins", "log_tail": "", "status": "", "errors": [], "web_url": web_url}
    if not (user and token and web_url):
        evidence["errors"].append("missing jenkins creds or build url")
        return evidence
    try:
        console = requests.get(f"{web_url}/consoleText", auth=(user, token),
                               timeout=REQUEST_TIMEOUT).text
        evidence["log_tail"] = console[-CI_LOG_TAIL:]
        evidence["status"] = "failed" if "FAILURE" in console[-500:] else "unknown"
    except Exception as e:
        evidence["errors"].append(str(e))
    return evidence


def _project_id_from_url(url: str):
    from urllib.parse import quote
    m = re.search(r"/projects/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"gitlab[^/]*/(.+?)/-/", url)
    if m:
        return quote(m.group(1).strip("/"), safe="")
    return None


# ------------------------------------------------------ CI signature matching

def _match_ci_signatures(ci_evidence: dict, findings: list):
    """Catch failures visible only in the CI log — before any pod exists."""
    log = (ci_evidence.get("log_tail") or "").lower()
    Finding = diag.Finding

    patterns = [
        ("helm upgrade failed", "HelmUpgradeFailed", "critical",
         "Helm upgrade failed in the pipeline",
         "The helm upgrade step exited non-zero — the release may be stuck. "
         "Check `helm history` and roll back to the last good revision.", "helm"),
        ("timed out waiting for the condition", "RolloutTimeout", "critical",
         "Rollout timed out",
         "New pods never became ready within the timeout — usually a crash loop "
         "or failing readiness probe on the new version. Check pod logs below.", "k8s"),
        ("error: unable to recognize", "InvalidManifest", "critical",
         "Invalid Kubernetes manifest",
         "kubectl rejected the manifest — a bad CRD, wrong apiVersion, or malformed "
         "YAML from the chart render. Check the values used for this deploy.", "config"),
        ("no matching manifest for", "ImageArchMismatch", "critical",
         "Image architecture mismatch",
         "The image exists but not for the target node architecture. "
         "Check the build platform vs node arch.", "config"),
        ("permission denied", "CIPermissionDenied", "critical",
         "Permission denied in pipeline",
         "The CI job lacks permission — a token scope or RBAC role is missing for "
         "the deploy step.", "config"),
        ("tests failed", "TestStageFailed", "warning",
         "Test stage failed",
         "The pipeline failed at the test stage, before deploy. This is app/test "
         "code, not infrastructure — check the test output above.", "config"),
    ]
    for needle, sig, sev, title, fix, kind in patterns:
        if needle in log:
            findings.append(Finding(sig, sev, title,
                                    f"CI log contains: '{needle}'", fix, kind))
    return findings


# ------------------------------------------------------------- LLM correlation

def _llm_correlate(dep, ci_evidence, cluster_ev, findings):
    import json
    from llm_client import complete

    payload = {
        "deploy": {"app": dep["app_name"], "ci": dep["ci"],
                   "pipeline_ref": dep["ref_id"], "cluster": dep["cluster"],
                   "namespace": dep["namespace"]},
        "ci_log_tail": (ci_evidence.get("log_tail") or "")[-2500:],
        "ci_status": ci_evidence.get("status"),
        "cluster": {
            "pods": cluster_ev.pods,
            "recent_events": cluster_ev.events[-10:],
            "helm_status": cluster_ev.helm_status,
            "missing_references": cluster_ev.missing_refs,
            "pod_log_tail": cluster_ev.logs_tail[-1500:],
        },
        "rule_findings": [diag.asdict(f) for f in findings],
    }
    prompt = (
        "You are diagnosing a failed deployment. You have BOTH the CI pipeline log "
        "and the cluster state, tied together by one deploy. Rule-based checks are "
        "included as rule_findings.\n\n"
        "Determine where the failure actually originated — pipeline stage (build/test/"
        "deploy) or cluster (pod/helm/config) — and give the single most likely root "
        "cause in 1-2 sentences, then ordered remediation steps a human can take. "
        "Correlate the two sides: e.g. a helm timeout in CI plus a CrashLoopBackOff in "
        "the cluster is ONE story, not two. Use only what's in the evidence.\n\n"
        f"EVIDENCE:\n{json.dumps(payload, indent=2)}"
    )
    return complete(prompt)


# ------------------------------------------------------------------ mock data

def _mock_ci_evidence(dep: dict) -> dict:
    """CI traces matched to the mock cluster scenarios in diagnostics.py."""
    app = dep["app_name"]
    traces = {
        "api-service": {
            "ci": "gitlab", "status": "success", "job_name": "deploy-prod", "errors": [],
            "log_tail": ("$ helm upgrade api-service ./chart --set image.tag=26.07.9\n"
                         "Release \"api-service\" has been upgraded. Happy Helming!\n"
                         "$ kubectl rollout status deploy/api-service\n"
                         "Waiting for deployment rollout to finish: 0 of 3 updated replicas...\n"
                         "error: timed out waiting for the condition"),
        },
        "ui-app": {
            "ci": "gitlab", "status": "success", "job_name": "deploy-prod", "errors": [],
            "log_tail": ("$ helm upgrade ui-app ./chart\n"
                         "Release \"ui-app\" has been upgraded. Happy Helming!\n"
                         "deploy stage completed — pods scheduled"),
        },
        "batch-reconciler": {
            "ci": "jenkins", "status": "failed", "errors": [],
            "log_tail": ("+ helm upgrade batch-reconciler ./chart --wait\n"
                         "Error: UPGRADE FAILED: post-upgrade hooks failed: "
                         "job failed: BackoffLimitExceeded\n"
                         "secret \"batch-reconciler-db\" not found\n"
                         "Build step 'Execute shell' marked build as FAILURE"),
        },
    }
    return traces.get(app, {
        "ci": dep.get("ci", "gitlab"), "status": "failed", "errors": [],
        "log_tail": ("$ deploy step\nsome generic failure output\n"
                     "error: process exited with code 1"),
    })
