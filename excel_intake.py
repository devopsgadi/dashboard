"""
Excel intake for bulk deploy.

Expected columns (case-insensitive, extra columns ignored):
  app_name   — required
  branch     — required
  version    — optional (falls back to channel default / "latest")
  gitlab_url — GitLab pipeline/project URL (fill this OR jenkins_url)
  jenkins_url— Jenkins job URL

Each row is validated independently so a bad row never blocks the good ones.
parse_excel returns (rows, ci) where each row carries a `_valid` flag and
`_issues` list the UI renders. Only `_valid` rows are eligible to trigger.
"""

import io

import pandas as pd

from ci_triggers import choose_ci

REQUIRED = ["app_name", "branch"]
KNOWN = ["app_name", "branch", "version", "gitlab_url", "jenkins_url"]


def parse_excel(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Returns (rows, global_errors). Rows always returned even if some are invalid."""
    try:
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
    except Exception as e:
        return [], [f"Could not read spreadsheet: {e}"]

    # normalize headers: lowercase, strip, spaces->underscores
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    missing_cols = [c for c in REQUIRED if c not in df.columns]
    if missing_cols:
        return [], [f"Missing required column(s): {', '.join(missing_cols)}. "
                    f"Expected headers: {', '.join(KNOWN)}"]

    df = df.where(pd.notna(df), None)   # NaN -> None

    rows = []
    for i, r in df.iterrows():
        row = {
            "app_name": _clean(r.get("app_name")),
            "branch": _clean(r.get("branch")),
            "version": _clean(r.get("version")),
            "gitlab_url": _clean(r.get("gitlab_url")),
            "jenkins_url": _clean(r.get("jenkins_url")),
            "_row": int(i) + 2,   # +2: header is row 1, data starts at 2
        }
        row["_issues"] = _validate(row)
        row["_valid"] = not row["_issues"]
        row["ci"] = choose_ci(row["gitlab_url"], row["jenkins_url"])
        rows.append(row)

    return rows, []


def _clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none") else s


def _validate(row: dict) -> list[str]:
    issues = []
    if not row["app_name"]:
        issues.append("app_name is empty")
    if not row["branch"]:
        issues.append("branch is empty")
    if not row["gitlab_url"] and not row["jenkins_url"]:
        issues.append("no CI URL — set gitlab_url or jenkins_url")
    if row["gitlab_url"] and not row["gitlab_url"].startswith(("http://", "https://")):
        issues.append("gitlab_url is not a valid URL")
    if row["jenkins_url"] and not row["jenkins_url"].startswith(("http://", "https://")):
        issues.append("jenkins_url is not a valid URL")
    return issues


def summarize(rows: list[dict]) -> dict:
    valid = [r for r in rows if r["_valid"]]
    return {
        "total": len(rows),
        "valid": len(valid),
        "invalid": len(rows) - len(valid),
        "gitlab": sum(1 for r in valid if r["ci"] == "gitlab"),
        "jenkins": sum(1 for r in valid if r["ci"] == "jenkins"),
    }
