# Deployment Dashboard

Self-contained Streamlit ops dashboard. Runs immediately on mock data — no cluster, CI, or LLM required.

## Quickstart

```bash
./run.sh                 # mock mode: creates venv, installs deps, launches
```

Then open http://localhost:8501. Or manually:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Try these first: upload `sample_deployments.xlsx` on **Bulk Deploy**, and run a diagnosis on `api-service` (wired to show an ImagePullBackOff) on **Diagnostics**.

## What's in this folder

```
run.sh                     one-command launcher (mock or --live)
app.py                     UI + router (9 pages)
config.py                  APPS registry — edit to add your apps
credentials.json           starter (empty = mock); real secrets go here for live
credentials.example.json   annotated template
params/                    per-channel deploy params (digital-banking, internal-tools)
sample_deployments.xlsx    example upload for Bulk Deploy
.streamlit/config.toml     theme
requirements.txt
```

Mock data is built in — deployment history seeds itself, diagnostics show real failure
scenarios, and every CI/kubectl/LLM call returns realistic fakes until you go live.

## Going live

```bash
./run.sh --live          # sets DASHBOARD_USE_MOCK=false, checks kubectl/helm
```

Fill `credentials.json` first (see the block-by-block notes below and in
`credentials.example.json`), and `chmod 600 credentials.json`. Both `credentials.json`
and `params/` are gitignored so real secrets never commit — `credentials.example.json`
is the template you share.

---

## Going live

```bash
cp credentials.example.json credentials.json
$EDITOR credentials.json
chmod 600 credentials.json          # app warns on startup if you skip this
export DASHBOARD_USE_MOCK=false
```

`credentials.json` holds the GitLab token, per-cluster kubectl contexts and kubeconfig paths, and the Rancher/Kibana base URLs. It's gitignored. Env vars still work as a fallback if you'd rather not use the file.

**1. GitLab — `backend.trigger_pipeline()`**
POSTs to `/api/v4/projects/{id}/pipeline` with `ref` = branch and the form params as CI variables. Your `.gitlab-ci.yml` reads them as `$ENV`, `$VERSION`, `$SKIP_TESTS`, `$ROLLBACK`. Set each app's real `gitlab_project_id` in `config.py`.

**2. kubectl — `backend.run_kubectl()`**
Runs `kubectl --context <cluster> <args>` via subprocess. Needs a kubeconfig readable by the dashboard process with contexts matching the `cluster` field of each app. In-cluster, mount a ServiceAccount with a read-only ClusterRole (`get`/`list` on pods, deployments, events, and `pods/log`).

## Onboarding an app

Add an entry to `APPS` in `config.py`:

```python
"my-service": {
    "namespace": "prod-api",
    "cluster": "aks-prod-eastus",
    "gitlab_project_id": 1099,
    "deploy_job": "deploy-prod",
    "rancher_url": f"{RANCHER_BASE}/dashboard/c/aks-prod-eastus/explorer/apps.deployment/prod-api/my-service",
    "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-api'))",
},
```

Every page picks it up automatically — no other changes needed.

## Notes on the terminal page

Commands are **not** free-form. `KUBECTL_ACTIONS` in `config.py` is an allowlist of argv templates; only `{ns}` and `{app}` are substituted, and subprocess runs without a shell. Add actions there rather than exposing an input box — that's what keeps this safe to hand to the wider team.

## Deployment

Behind an authenticating reverse proxy (this app has no auth of its own):

```bash
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Container:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update && apt-get install -y curl && \
    curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address", "0.0.0.0"]
```

## Answering the three open questions

- **Auto-refresh** — built in, sidebar toggle (30s). Off by default.
- **Dark mode** — handled by the theme; all custom CSS uses translucent colors that track both modes. Users toggle via ☰ → Settings → Theme.
- **Company logo** — drop a `logo.png` next to `app.py` and add `st.logo("logo.png")` at the top of the sidebar block.

## Diagnose by deploy ID

Every Bulk/Batch deploy now stamps each app with a dashboard deploy ID (e.g.
`dep-3687abdcc8`), stored with its CI system, pipeline/build ref, and cluster.

On the **Diagnostics** page, the "Diagnose by deploy ID" tab takes that ID and:

1. Resolves it to the app, CI system, pipeline ref, and cluster (a DB lookup).
2. Pulls the **CI job log** — GitLab job trace or Jenkins console — for that exact run.
3. Collects the **cluster state** (pods, events, helm status, pod logs) via the existing collector.
4. Runs signature rules over **both** sides, then a single LLM pass correlates them.

The point of the ID is correlation. Half of deploy failures never reach a pod — a
failed `helm upgrade`, a rejected manifest, a test-stage failure — so the CI trace is
often where the real error lives. And when both sides fail, they're usually one story:
a rollout timeout in CI *because* of an ImagePullBackOff in the cluster. The combined
view catches that; neither side alone does.

This is collect-then-reason: deterministic gathering, one explanation pass. The LLM
never runs commands and never receives credentials — it reasons over evidence already
assembled and returns text. Read-only by construction, same as the app-inspection tab.

CI-side signatures caught before any pod exists: HelmUpgradeFailed, RolloutTimeout,
InvalidManifest, ImageArchMismatch, CIPermissionDenied, TestStageFailed. Add more in
`_match_ci_signatures()` in `id_diagnosis.py`.
