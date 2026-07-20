"""
DevOps Deployment Dashboard
---------------------------
Single-file Streamlit app: Overview / Deploy / Kubernetes Terminal / Monitoring.

Runs out of the box against mock data. To go live, flip USE_MOCK = False in
config.py and fill in the two integration points in backend.py:
  - trigger_pipeline()  -> GitLab CI pipeline trigger
  - run_kubectl()       -> kubectl exec against your clusters

    streamlit run app.py
"""

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from config import APPS, DEPLOY_PARAMS, DEFAULT_BRANCH, KUBECTL_ACTIONS
from backend import (
    get_app_stats,
    trigger_pipeline,
    run_kubectl,
    record_deployment,
    init_db,
    get_audit_log,
    current_actor,
)
import actions as act
import credentials as creds
import batch as batchmod
import diagnostics as diag
import id_diagnosis
import llm_client
import excel_intake
import channels

# ------------------------------------------------------------------ page setup

st.set_page_config(
    page_title="Deployment Dashboard",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_db()

# Accent styling. Uses CSS vars so it tracks Streamlit's light/dark theme
# instead of hardcoding backgrounds that break in the other mode.
st.markdown(
    """
    <style>
      :root {
        --accent-blue: #2563eb;
        --accent-green: #16a34a;
        --accent-red: #dc2626;
      }
      .block-container { padding-top: 2.2rem; max-width: 1400px; }

      .metric-card {
        border: 1px solid rgba(128,128,128,0.25);
        border-radius: 10px;
        padding: 1.1rem 1.2rem;
        border-left: 4px solid var(--accent-blue);
        height: 100%;
      }
      .metric-card.good  { border-left-color: var(--accent-green); }
      .metric-card.bad   { border-left-color: var(--accent-red); }
      .metric-label {
        font-size: 0.78rem; text-transform: uppercase;
        letter-spacing: 0.06em; opacity: 0.65; font-weight: 600;
      }
      .metric-value { font-size: 2.1rem; font-weight: 700; line-height: 1.2; }
      .metric-value.good { color: var(--accent-green); }
      .metric-value.bad  { color: var(--accent-red); }
      .metric-sub { font-size: 0.8rem; opacity: 0.6; }

      .pill {
        display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
        font-size: 0.75rem; font-weight: 600;
      }
      .pill-ok   { background: rgba(22,163,74,0.15);  color: var(--accent-green); }
      .pill-fail { background: rgba(220,38,38,0.15);  color: var(--accent-red); }

      div[data-testid="stSidebarNav"] { display: none; }
      section[data-testid="stSidebar"] .stRadio label { font-size: 0.95rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def metric_card(label, value, sub="", tone=""):
    st.markdown(
        f"""
        <div class="metric-card {tone}">
          <div class="metric-label">{label}</div>
          <div class="metric-value {tone}">{value}</div>
          <div class="metric-sub">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown("### 🚀 Deployment Dashboard")
    st.caption("Platform Engineering")
    st.divider()

    page = st.radio(
        "Navigation",
        ["Overview", "Deploy", "Batch Deploy", "Bulk Deploy (Excel)", "Diagnostics",
         "Kubernetes Terminal", "Actions", "Monitoring", "Audit Log"],
        label_visibility="collapsed",
    )

    st.divider()
    st.caption(f"Signed in as **{current_actor()}**")
    auto_refresh = st.toggle("Auto-refresh (30s)", value=False)
    if st.button("↻ Refresh now", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}")

if auto_refresh:
    # Lightweight polling loop — reruns the script every 30s.
    st.markdown(
        "<meta http-equiv='refresh' content='30'>", unsafe_allow_html=True
    )


# ------------------------------------------------------------------ Overview

def page_overview():
    st.title("Overview")
    st.caption("Deployment health across all applications")

    stats = get_app_stats()
    df = pd.DataFrame(stats)

    total_apps = len(df)
    success = int(df["success_count"].sum())
    failure = int(df["failure_count"].sum())
    total_runs = success + failure
    rate = (success / total_runs * 100) if total_runs else 0.0
    failing_now = int((df["last_status"] == "Failed").sum())

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        metric_card("Total Apps", total_apps, f"{failing_now} currently failing")
    with c2:
        metric_card("Successful Deployments", f"↑ {success}", "all-time", "good")
    with c3:
        metric_card("Failed Deployments", f"↓ {failure}", "all-time", "bad")
    with c4:
        tone = "good" if rate >= 80 else "bad"
        metric_card("Success Rate", f"{rate:.1f}%", f"{total_runs} total runs", tone)

    st.write("")
    st.subheader("Applications")

    view = pd.DataFrame({
        "app_name": df["app_name"],
        "last_status": df["last_status"],
        "last_deployed": df["last_deployed"],
        "success / failure": df["success_count"].astype(str) + " / " + df["failure_count"].astype(str),
        "Rancher": df["rancher_url"],
        "Kibana": df["kibana_url"],
    })

    def highlight_failed(row):
        if row["last_status"] == "Failed":
            return ["background-color: rgba(220,38,38,0.12)"] * len(row)
        return [""] * len(row)

    st.dataframe(
        view.style.apply(highlight_failed, axis=1),
        width="stretch",
        hide_index=True,
        column_config={
            "app_name": st.column_config.TextColumn("App", width="medium"),
            "last_status": st.column_config.TextColumn("Last Status", width="small"),
            "last_deployed": st.column_config.TextColumn("Last Deployed", width="medium"),
            "success / failure": st.column_config.TextColumn("Success / Fail", width="small"),
            "Rancher": st.column_config.LinkColumn("Rancher", display_text="🌐 Open", width="small"),
            "Kibana": st.column_config.LinkColumn("Kibana", display_text="📊 Open", width="small"),
        },
    )

    if failing_now:
        names = ", ".join(df.loc[df["last_status"] == "Failed", "app_name"])
        st.error(f"**{failing_now} app(s) in a failed state:** {names}")


# -------------------------------------------------------------------- Deploy

def page_deploy():
    st.title("Deploy")
    st.caption("Trigger a production deployment pipeline")

    left, right = st.columns([2, 1])

    with left:
        app_name = st.selectbox("Application", list(APPS.keys()))
        branch = st.text_input("Branch", value=DEFAULT_BRANCH)

        st.markdown("**Pipeline parameters**")
        params = {}
        pcols = st.columns(2)
        for i, (key, spec) in enumerate(DEPLOY_PARAMS.items()):
            with pcols[i % 2]:
                if spec["type"] == "select":
                    params[key] = st.selectbox(key, spec["options"], key=f"p_{key}")
                elif spec["type"] == "bool":
                    params[key] = st.checkbox(key, value=spec["default"], key=f"p_{key}")
                else:
                    params[key] = st.text_input(key, value=spec["default"], key=f"p_{key}")

    with right:
        meta = APPS[app_name]
        st.markdown("**Target**")
        st.info(
            f"**Namespace:** `{meta['namespace']}`\n\n"
            f"**Cluster:** `{meta['cluster']}`\n\n"
            f"**Project ID:** `{meta['gitlab_project_id']}`"
        )

    st.write("")

    if params.get("ENV") == "prod":
        st.warning("This targets **production**. Confirm before triggering.")
        confirmed = st.checkbox(f"I confirm deploying `{app_name}` from `{branch}` to production")
    else:
        confirmed = True

    if st.button("🚀 Trigger Deployment to Production",
                 type="primary", width="stretch", disabled=not confirmed):
        with st.spinner(f"Triggering pipeline for {app_name}…"):
            result = trigger_pipeline(app_name, branch, params)

        if result.get("ok"):
            st.success(
                f"Pipeline **#{result['pipeline_id']}** triggered for `{app_name}` on `{branch}`"
            )
            record_deployment(app_name, "Success")
        else:
            st.error(f"Trigger failed: {result.get('error', 'unknown error')}")
            record_deployment(app_name, "Failed")

        st.markdown("**Response**")
        st.json(result)


# --------------------------------------------------------- Kubernetes Terminal

def page_terminal():
    st.title("Kubernetes Terminal")
    st.caption("Read-only cluster inspection — allowlisted commands only")

    app_name = st.selectbox("Application", list(APPS.keys()))
    meta = APPS[app_name]

    c1, c2 = st.columns(2)
    c1.info(f"**Namespace:** `{meta['namespace']}`")
    c2.info(f"**Cluster:** `{meta['cluster']}`")

    st.write("")
    cols = st.columns(len(KUBECTL_ACTIONS))
    clicked = None
    for col, (label, _) in zip(cols, KUBECTL_ACTIONS.items()):
        if col.button(label, width="stretch", key=f"k_{label}"):
            clicked = label

    if clicked:
        st.session_state["last_action"] = clicked

    action = st.session_state.get("last_action")
    if action:
        with st.spinner(f"Running {action.lower()}…"):
            cmd, output = run_kubectl(action, app_name)

        st.markdown(f"**`$ {cmd}`**")
        st.code(output, language="bash")
    else:
        st.caption("Pick an action above to run it against the selected app.")


# ---------------------------------------------------------------- Monitoring

def page_monitoring():
    st.title("Monitoring")
    st.caption("Jump into Rancher or Kibana for any application")

    stats = {s["app_name"]: s for s in get_app_stats()}

    for app_name, meta in APPS.items():
        s = stats.get(app_name, {})
        status = s.get("last_status", "Unknown")
        pill = "pill-ok" if status == "Success" else "pill-fail"

        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.markdown(
                    f"**{app_name}** &nbsp; "
                    f"<span class='pill {pill}'>{status}</span>",
                    unsafe_allow_html=True,
                )
                st.caption(f"`{meta['namespace']}` · `{meta['cluster']}`")
            c2.link_button("🌐 Rancher", meta["rancher_url"], width="stretch")
            c3.link_button("📊 Kibana", meta["kibana_url"], width="stretch")


# ------------------------------------------------------------------- Actions

def page_actions():
    st.title("Actions")
    st.caption("Cluster write operations — every action is audited")

    actor = current_actor()
    if actor == "unknown":
        st.warning(
            "**No user identity available.** These actions run under a shared service "
            "account, so the audit log can't attribute them to a person. Put this app "
            "behind an SSO proxy that sets `X-Forwarded-User` to fix it."
        )

    app_name = st.selectbox("Application", list(APPS.keys()), key="act_app")
    meta = APPS[app_name]

    c1, c2 = st.columns(2)
    c1.info(f"**Namespace:** `{meta['namespace']}`")
    c2.info(f"**Cluster:** `{meta['cluster']}`")

    st.divider()

    action_key = st.radio(
        "Action",
        list(act.ACTIONS.keys()),
        format_func=lambda k: f"{act.ACTIONS[k]['icon']}  {act.ACTIONS[k]['label']}",
        horizontal=True,
    )
    spec = act.ACTIONS[action_key]
    st.caption(spec["description"])

    params = {}

    # --- forms rendered from the param spec
    for pname, p in spec["params"].items():
        if p["type"] == "int":
            current = act.current_replicas(app_name)
            if current is not None:
                st.caption(f"Current: **{current}** replica(s)")
            params[pname] = st.number_input(
                p["label"], min_value=p["min"], max_value=p["max"],
                value=current if current is not None else 1, step=1,
            )
        elif p["type"] == "select_secret":
            secrets = act.list_secrets(app_name)
            if not secrets:
                st.error("No secrets found in this namespace.")
                return
            params[pname] = st.selectbox(p["label"], secrets)
        elif p["type"] == "select_key":
            keys = act.secret_keys(app_name, params.get("secret_name", ""))
            if not keys:
                st.error("Couldn't read keys from that secret.")
                return
            params[pname] = st.selectbox(p["label"], keys)
        elif p["type"] == "password":
            params[pname] = st.text_input(p["label"], type="password")
        elif p["type"] == "select_revision":
            hist = act.helm_history(app_name)
            if not hist:
                st.error("No Helm history found for this release.")
                return
            st.dataframe(
                pd.DataFrame(hist)[["revision", "updated", "status", "chart"]],
                hide_index=True, width="stretch",
            )
            older = [h for h in hist if h.get("status") != "deployed"]
            params[pname] = st.selectbox(
                p["label"],
                [h["revision"] for h in older],
                format_func=lambda r: f"Revision {r}",
            )

    st.write("")

    # --- confirmation gate, scaled to blast radius
    ready = True
    if spec["confirm"] == "typed":
        typed = st.text_input(f"Type `{app_name}` to confirm")
        ready = typed == app_name
    else:
        ready = st.checkbox(f"I confirm this {spec['label'].lower()} on **{app_name}**")

    if action_key == "scale" and params.get("replicas") == 0:
        st.error("Scaling to 0 takes this app fully offline.")

    if st.button(f"Run: {spec['label']}", type="primary", width="stretch", disabled=not ready):
        with st.spinner("Running…"):
            result = act.run_action(action_key, app_name, params, actor)

        if result.ok:
            st.success(result.summary)
        else:
            st.error(result.summary)

        if result.command:
            st.code(result.command, language="bash")
        if result.detail:
            st.code(result.detail, language="bash")


# ----------------------------------------------------------------- Audit Log

def page_audit():
    st.title("Audit log")
    st.caption("Every write action attempted through this dashboard")

    rows = get_audit_log(100)
    if not rows:
        st.info("No actions recorded yet.")
        return

    df = pd.DataFrame(rows)

    def highlight_failed(row):
        if row["result"] == "FAILED":
            return ["background-color: rgba(220,38,38,0.12)"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df.style.apply(highlight_failed, axis=1),
        hide_index=True, width="stretch",
        column_config={
            "when": st.column_config.TextColumn("When", width="medium"),
            "actor": st.column_config.TextColumn("Actor", width="medium"),
            "action": st.column_config.TextColumn("Action", width="small"),
            "app": st.column_config.TextColumn("App", width="medium"),
            "params": st.column_config.TextColumn("Params", width="medium"),
            "result": st.column_config.TextColumn("Result", width="small"),
            "detail": st.column_config.TextColumn("Detail", width="large"),
        },
    )
    st.caption(f"Credentials source: `{creds.source()}`")


# -------------------------------------------------------------- Batch Deploy

def _state_pill(state):
    colors = {
        "queued": ("var(--fill-secondary)", "#888"),
        "running": ("rgba(37,99,235,0.15)", "#2563eb"),
        "success": ("rgba(22,163,74,0.15)", "#16a34a"),
        "failed": ("rgba(220,38,38,0.15)", "#dc2626"),
    }
    bg, fg = colors.get(state, colors["queued"])
    icon = {"queued": "○", "running": "◐", "success": "●", "failed": "✕"}.get(state, "○")
    return f"<span class='pill' style='background:{bg};color:{fg}'>{icon} {state}</span>"


def page_batch():
    st.title("Batch Deploy")
    st.caption("Trigger multiple pipelines in parallel and watch them live")

    selected = st.multiselect("Applications", list(APPS.keys()),
                              default=list(APPS.keys())[:3])
    c1, c2 = st.columns(2)
    branch = c1.text_input("Branch", value=DEFAULT_BRANCH)
    env = c2.selectbox("ENV", ["prod", "staging", "dr"])

    params = {"ENV": env, "VERSION": "latest", "SKIP_TESTS": False, "ROLLBACK": False}

    if env == "prod":
        confirmed = st.checkbox(f"I confirm deploying {len(selected)} app(s) to production")
    else:
        confirmed = True

    if st.button("🚀 Trigger Batch Deployment", type="primary", width="stretch",
                 disabled=not selected or not confirmed):
        batch = batchmod.new_batch(selected, branch, params)
        with st.spinner(f"Triggering {len(selected)} pipelines in parallel…"):
            batch = batchmod.execute_batch(batch)
        st.session_state["last_batch"] = batch

    batch = st.session_state.get("last_batch")
    if not batch:
        st.info("Select apps and trigger a batch to see the live status grid.")
        return

    s = batch.summary
    m1, m2, m3, m4 = st.columns(4)
    with m1: metric_card("Total", s["total"])
    with m2: metric_card("Succeeded", s["success"], tone="good")
    with m3: metric_card("Failed", s["failed"], tone="bad")
    with m4: metric_card("Pending", s["pending"])

    st.write("")
    st.subheader("Status grid")
    for name, job in batch.jobs.items():
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 3, 1])
            c1.markdown(f"**{name}**", unsafe_allow_html=True)
            c1.markdown(_state_pill(job.state), unsafe_allow_html=True)
            c2.caption(job.message or "—")
            if job.state == "failed":
                c3.button("🔍 Diagnose", key=f"diag_{name}",
                          on_click=lambda n=name: st.session_state.update(
                              {"diag_app": n, "_goto": "Diagnostics"}))
            elif job.web_url:
                c3.link_button("Pipeline", job.web_url, width="stretch")

    if any(j.state == "failed" for j in batch.jobs.values()):
        st.warning("Some deployments failed. Use **Diagnose** to run the LLM root-cause analysis.")


# --------------------------------------------------------------- Diagnostics

SEV_STYLE = {
    "critical": ("rgba(220,38,38,0.10)", "#dc2626", "🔴"),
    "warning": ("rgba(217,119,6,0.10)", "#d97706", "🟠"),
    "info": ("rgba(37,99,235,0.10)", "#2563eb", "🔵"),
}


def page_diagnostics():
    st.title("Diagnostics")
    st.caption("Paste a failed deploy ID for full CI + cluster root-cause, or inspect an app directly")

    tab_id, tab_app = st.tabs(["Diagnose by deploy ID", "Inspect an app"])

    with tab_id:
        _diagnostics_by_id()
    with tab_app:
        _diagnostics_by_app()


def _diagnostics_by_id():
    from backend import recent_failed_deploys

    failed = recent_failed_deploys(20)
    c1, c2 = st.columns([3, 2])
    deploy_id = c1.text_input("Deploy ID", placeholder="dep-xxxxxxxxxx",
                              value=st.session_state.get("diag_id", ""))
    if failed:
        pick = c2.selectbox("…or pick a recent failure",
                            ["—"] + [f"{d['deploy_id']} · {d['app_name']}" for d in failed])
        if pick != "—":
            deploy_id = pick.split(" · ")[0]
    else:
        c2.caption("No failed deploys yet. Run a Bulk Deploy to generate IDs.")

    use_llm = st.toggle("Include LLM correlation", value=True, key="id_llm")
    if not llm_client.configured() and use_llm:
        st.caption("LLM in mock mode — configure the `llm` block in credentials.json for live analysis.")

    if st.button("🔍 Diagnose deploy", type="primary", width="stretch",
                 disabled=not deploy_id):
        with st.spinner("Resolving ID, pulling CI log and cluster state…"):
            st.session_state["id_result"] = id_diagnosis.diagnose_by_id(deploy_id, use_llm)

    result = st.session_state.get("id_result")
    if not result:
        st.info("Paste a deploy ID (from Bulk/Batch deploy) to get combined CI + cluster analysis.")
        return
    if not result.get("ok"):
        st.error(result["error"])
        return

    dep = result["deploy"]
    m1, m2, m3, m4 = st.columns(4)
    with m1: metric_card("App", dep["app_name"])
    with m2: metric_card("CI", dep["ci"] or "—")
    with m3: metric_card("Pipeline", dep["ref_id"] or "—")
    with m4: metric_card("Status", dep["status"], tone="bad" if dep["status"] == "Failed" else "good")

    _render_findings(result["findings"])
    _render_llm(result.get("llm"), label="Combined CI + cluster analysis")

    with st.expander("CI pipeline log (tail)"):
        ci = result["ci_evidence"]
        if ci.get("errors"):
            st.warning("; ".join(ci["errors"]))
        st.code(ci.get("log_tail") or "(no CI log)", language="log")

    with st.expander("Cluster evidence"):
        _render_cluster_evidence(result["cluster_evidence"])


def _diagnostics_by_app():
    apps = list(APPS.keys())
    default_idx = apps.index(st.session_state["diag_app"]) if st.session_state.get("diag_app") in apps else 0
    app_name = st.selectbox("Application", apps, index=default_idx)

    c1, c2, c3 = st.columns([1, 1, 2])
    meta = APPS[app_name]
    c1.info(f"**Namespace:** `{meta['namespace']}`")
    c2.info(f"**Cluster:** `{meta['cluster']}`")
    use_llm = c3.toggle("Include LLM analysis", value=True)

    if not llm_client.configured() and use_llm:
        c3.caption("LLM in mock mode — configure the `llm` block in credentials.json for live analysis.")

    if st.button("🔍 Run Diagnosis", type="primary", width="stretch"):
        with st.spinner("Collecting evidence from the cluster…"):
            st.session_state["diag_result"] = diag.diagnose(app_name, use_llm=use_llm)

    result = st.session_state.get("diag_result")
    if not result or result["app"] != app_name:
        st.info("Run a diagnosis to see findings and root-cause analysis.")
        return

    _render_findings(result["findings"])
    _render_llm(result.get("llm"))
    with st.expander("Raw evidence collected"):
        _render_cluster_evidence(result["evidence"])


def _render_findings(findings):
    st.subheader("Findings")
    if not findings:
        st.success("No known failure signatures matched. App appears healthy, or the issue is novel — see LLM analysis.")
    for f in findings:
        bg, fg, icon = SEV_STYLE.get(f["severity"], SEV_STYLE["info"])
        st.markdown(
            f"""<div style="background:{bg};border-left:3px solid {fg};padding:10px 12px;border-radius:6px;margin-bottom:8px">
            <div style="font-weight:600;color:{fg}">{icon} {f['title']}
            <span style="font-size:0.7rem;opacity:0.7;text-transform:uppercase">· {f['kind']}</span></div>
            <div style="font-size:0.85rem;margin:4px 0"><b>Evidence:</b> {f['evidence']}</div>
            <div style="font-size:0.85rem"><b>Suggested fix:</b> {f['suggested_fix']}</div>
            </div>""",
            unsafe_allow_html=True,
        )


def _render_llm(llm, label="LLM root-cause analysis"):
    if not llm:
        return
    st.subheader(label)
    if llm.get("ok"):
        st.markdown(llm["text"])
        st.caption(f"Model: `{llm.get('model', 'unknown')}` · read-only, no actions taken")
    else:
        st.error(f"LLM analysis unavailable: {llm.get('error')}")


def _render_cluster_evidence(ev):
    if ev.get("pods"):
        st.markdown("**Pods**")
        st.dataframe(pd.DataFrame(ev["pods"]), hide_index=True, width="stretch")
    if ev.get("events"):
        st.markdown("**Recent events**")
        st.dataframe(pd.DataFrame(ev["events"]), hide_index=True, width="stretch")
    if ev.get("helm_status"):
        st.markdown("**Helm status**")
        st.json(ev["helm_status"])
    if ev.get("logs_tail"):
        st.markdown("**Pod log tail**")
        st.code(ev["logs_tail"], language="log")
    if ev.get("collect_errors"):
        st.warning("Collection errors: " + "; ".join(ev["collect_errors"]))


# -------------------------------------------------------- Bulk Deploy (Excel)

def _make_template_bytes():
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "deployments"
    headers = ["app_name", "branch", "version", "gitlab_url", "jenkins_url"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3864")
    examples = [
        ["api-service", "release_26.07", "26.07.1", "https://gitlab.example.com/banking/api-service", ""],
        ["payments-worker", "release_26.07", "3.2.0", "", "https://jenkins.example.com/job/payments-worker"],
    ]
    for r in examples:
        ws.append(r)
    for col, w in zip("ABCDE", [22, 16, 12, 48, 48]):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def page_bulk():
    st.title("Bulk Deploy (Excel)")
    st.caption("Upload a spreadsheet, pick a channel, deploy in waves")

    # --- channel + template row
    c1, c2 = st.columns([2, 1])
    channel = c1.selectbox("Project / channel", channels.available_channels())
    c2.download_button("⬇ Download template", _make_template_bytes(),
                       file_name="deployments_template.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       width="stretch")

    params = channels.load_params(channel)
    if params.get("_error"):
        st.error(params["_error"])
        return
    with st.expander(f"Parameters applied to every app in this channel — `{channel}`"):
        st.json(params)
        st.caption("VERSION comes per-row from the Excel; these params come from the channel JSON.")

    # --- upload
    uploaded = st.file_uploader("Deployment spreadsheet (.xlsx)", type=["xlsx"])
    if not uploaded:
        st.info("Upload a spreadsheet with columns: **app_name, branch, version, gitlab_url, jenkins_url**. "
                "Fill either gitlab_url or jenkins_url per row.")
        return

    rows, errs = excel_intake.parse_excel(uploaded.getvalue())
    if errs:
        for e in errs:
            st.error(e)
        return

    s = excel_intake.summarize(rows)
    m1, m2, m3, m4 = st.columns(4)
    with m1: metric_card("Rows", s["total"])
    with m2: metric_card("Valid", s["valid"], tone="good")
    with m3: metric_card("Invalid", s["invalid"], tone="bad" if s["invalid"] else "")
    with m4: metric_card("GitLab / Jenkins", f"{s['gitlab']} / {s['jenkins']}")

    # --- preview grid
    st.subheader("Preview")
    preview = pd.DataFrame([{
        "row": r["_row"], "app_name": r["app_name"], "branch": r["branch"],
        "version": r["version"] or "(channel default)", "ci": r["ci"],
        "status": "✓ valid" if r["_valid"] else "✕ " + "; ".join(r["_issues"]),
    } for r in rows])

    def hl(row):
        if row["status"].startswith("✕"):
            return ["background-color: rgba(220,38,38,0.12)"] * len(row)
        return [""] * len(row)

    st.dataframe(preview.style.apply(hl, axis=1), hide_index=True, width="stretch")

    if s["valid"] == 0:
        st.error("No valid rows to deploy. Fix the flagged rows and re-upload.")
        return

    # --- controls
    st.subheader("Run settings")
    c1, c2 = st.columns(2)
    batch_size = c1.number_input("Batch size (apps per wave)", min_value=1,
                                 max_value=max(1, s["valid"]), value=min(5, s["valid"]))
    on_failure = c2.selectbox("On failure", ["continue", "stop"],
                              format_func=lambda x: {"continue": "Skip & continue",
                                                     "stop": "Stop after failed wave"}[x])
    n_waves = (s["valid"] + batch_size - 1) // batch_size
    st.caption(f"{s['valid']} apps in **{n_waves} wave(s)** of up to {batch_size}. "
               "Apps within a wave run in parallel; waves run in sequence.")

    prod = params.get("ENV") == "prod"
    confirmed = st.checkbox(f"Confirm bulk deploy of {s['valid']} app(s) to **{params.get('ENV','?')}**") if prod else True

    if st.button("🚀 Start Bulk Deploy", type="primary", width="stretch", disabled=not confirmed):
        run = batchmod.new_bulk_run(rows, channel, params, int(batch_size), on_failure)
        st.session_state["bulk_run"] = run
        st.session_state["bulk_wave"] = 0

    # --- execution + live grid
    run = st.session_state.get("bulk_run")
    if not run:
        return

    # drive waves one per rerun so the grid updates between them
    wave_idx = st.session_state.get("bulk_wave", 0)
    if wave_idx < len(run.waves) and not run.stopped:
        with st.spinner(f"Deploying wave {wave_idx + 1} of {len(run.waves)}…"):
            batchmod.execute_wave(run, wave_idx)
        st.session_state["bulk_wave"] = wave_idx + 1
        st.rerun()

    rs = run.summary
    st.subheader("Results")
    m1, m2, m3, m4 = st.columns(4)
    with m1: metric_card("Succeeded", rs["success"], tone="good")
    with m2: metric_card("Failed", rs["failed"], tone="bad")
    with m3: metric_card("Skipped", rs["skipped"])
    with m4: metric_card("Total", rs["total"])

    if run.stopped:
        st.warning("Run stopped after a failed wave. Remaining apps were skipped.")

    for wi, wave in enumerate(run.waves):
        st.markdown(f"**Wave {wi + 1}**")
        for job in wave:
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 1])
                c1.markdown(f"**{job.app_name}** &nbsp;<span style='font-size:0.7rem;opacity:0.6'>{job.ci}</span>",
                            unsafe_allow_html=True)
                c1.markdown(_state_pill(job.state), unsafe_allow_html=True)
                c2.caption(job.message or "—")
                if job.state == "failed":
                    c3.button("🔍 Diagnose", key=f"bdiag_{job.app_name}_{wi}",
                              on_click=lambda n=job.app_name: st.session_state.update({"diag_app": n}))
                elif job.web_url:
                    c3.link_button("Open", job.web_url, width="stretch")


# ------------------------------------------------------------------- router

PAGES = {
    "Overview": page_overview,
    "Deploy": page_deploy,
    "Batch Deploy": page_batch,
    "Bulk Deploy (Excel)": page_bulk,
    "Diagnostics": page_diagnostics,
    "Kubernetes Terminal": page_terminal,
    "Actions": page_actions,
    "Monitoring": page_monitoring,
    "Audit Log": page_audit,
}
PAGES[page]()
