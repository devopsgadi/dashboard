"""
CI triggers — GitLab and Jenkins behind one interface.

Each Excel row carries a gitlab_url and/or jenkins_url. Whichever is present
decides where that row fires. Params are NOT per-row: they come from the
selected project/channel's JSON (see channels.py) and apply to every row.

Both trigger_* functions return the same dict shape so the batch orchestrator
doesn't care which CI system ran:
  {ok, ci, pipeline_id, web_url, status, error}
"""

import re

import credentials as creds
from config import USE_MOCK

REQUEST_TIMEOUT = 25


def choose_ci(gitlab_url: str, jenkins_url: str) -> str:
    """Which CI system this row targets. GitLab wins if both are set."""
    if gitlab_url and gitlab_url.strip():
        return "gitlab"
    if jenkins_url and jenkins_url.strip():
        return "jenkins"
    return "none"


def trigger(row: dict, params: dict) -> dict:
    """Dispatch one row to its CI system. `row` has app_name, branch, version, urls."""
    ci = choose_ci(row.get("gitlab_url", ""), row.get("jenkins_url", ""))
    merged = {**params, "VERSION": row.get("version") or params.get("VERSION", "latest")}

    if ci == "gitlab":
        return trigger_gitlab(row, merged)
    if ci == "jenkins":
        return trigger_jenkins(row, merged)
    return {"ok": False, "ci": "none", "error": "row has neither gitlab_url nor jenkins_url"}


# --------------------------------------------------------------------- GitLab

def _gitlab_project_from_url(url: str):
    """
    Pull the project path or ID out of a GitLab URL.
    Accepts …/api/v4/projects/1042/… or …/group/app/-/pipelines style URLs.
    """
    m = re.search(r"/projects/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"gitlab[^/]*/(.+?)(?:/-/|/pipelines|$)", url)
    if m:
        from urllib.parse import quote
        return quote(m.group(1).strip("/"), safe="")
    return None


def trigger_gitlab(row: dict, params: dict) -> dict:
    branch = row.get("branch", "")
    app = row.get("app_name", "")
    url = row.get("gitlab_url", "")

    if USE_MOCK:
        import random
        pid = random.randint(100000, 999999)
        ok = random.random() < 0.85
        return {"ok": ok, "ci": "gitlab", "pipeline_id": pid,
                "web_url": f"{url.rstrip('/')}/-/pipelines/{pid}",
                "status": "created" if ok else "failed",
                "error": None if ok else "mock: no runner available"}

    import requests
    token = creds.get("gitlab", "token")
    base = creds.get("gitlab", "url", default="").rstrip("/")
    project = _gitlab_project_from_url(url)
    if not project:
        return {"ok": False, "ci": "gitlab", "error": f"couldn't parse project from {url}"}

    api = f"{base}/api/v4/projects/{project}/pipeline"
    try:
        r = requests.post(
            api,
            headers={"PRIVATE-TOKEN": token},
            json={"ref": branch,
                  "variables": [{"key": k, "value": str(v)} for k, v in params.items()]},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        return {"ok": True, "ci": "gitlab", "pipeline_id": d["id"],
                "web_url": d.get("web_url"), "status": d.get("status"), "error": None}
    except Exception as e:
        return {"ok": False, "ci": "gitlab", "error": str(e)}


# -------------------------------------------------------------------- Jenkins

def trigger_jenkins(row: dict, params: dict) -> dict:
    """
    Trigger a Jenkins job via buildWithParameters.

    jenkins_url in the Excel points at the job (…/job/<name>/). Auth is a user +
    API token from credentials.json under "jenkins". Params go as form fields.
    """
    app = row.get("app_name", "")
    url = row.get("jenkins_url", "").rstrip("/")

    if USE_MOCK:
        import random
        num = random.randint(100, 9999)
        ok = random.random() < 0.85
        return {"ok": ok, "ci": "jenkins", "pipeline_id": num,
                "web_url": f"{url}/{num}/",
                "status": "queued" if ok else "failed",
                "error": None if ok else "mock: job is disabled"}

    import requests
    user = creds.get("jenkins", "user")
    token = creds.get("jenkins", "api_token")
    if not (user and token):
        return {"ok": False, "ci": "jenkins", "error": "jenkins credentials missing in credentials.json"}

    build_url = f"{url}/buildWithParameters"
    form = {k: str(v) for k, v in params.items()}
    form.setdefault("BRANCH", row.get("branch", ""))
    try:
        r = requests.post(build_url, auth=(user, token), params=form, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        # Jenkins returns 201 with a Location header pointing at the queue item.
        queue = r.headers.get("Location", "")
        return {"ok": True, "ci": "jenkins", "pipeline_id": queue.rstrip("/").split("/")[-1] or "queued",
                "web_url": queue or url, "status": "queued", "error": None}
    except Exception as e:
        return {"ok": False, "ci": "jenkins", "error": str(e)}
