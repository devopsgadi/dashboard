"""
Configuration — edit this file to onboard apps and point at real systems.
"""

import os

import credentials as creds

# Flip to False once credentials.json (or the env vars) are in place.
USE_MOCK = os.getenv("DASHBOARD_USE_MOCK", "true").lower() == "true"

# credentials.json wins; env vars are the fallback.
GITLAB_URL = creds.get("gitlab", "url") or os.getenv("GITLAB_URL", "https://gitlab.example.com")
GITLAB_TOKEN = creds.get("gitlab", "token") or os.getenv("GITLAB_TOKEN", "")

RANCHER_BASE = creds.get("links", "rancher_base") or os.getenv("RANCHER_BASE", "https://rancher.example.com")
KIBANA_BASE = creds.get("links", "kibana_base") or os.getenv("KIBANA_BASE", "https://kibana.example.com")

DEFAULT_BRANCH = "release_26.07"

# --------------------------------------------------------------- app registry

APPS = {
    "api-service": {
        "namespace": "prod-api",
        "cluster": "aks-prod-eastus",
        "gitlab_project_id": 1042,
        "deploy_job": "deploy-prod",
        "rancher_url": f"{RANCHER_BASE}/dashboard/c/aks-prod-eastus/explorer/apps.deployment/prod-api/api-service",
        "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-api'))",
    },
    "ui-app": {
        "namespace": "prod-web",
        "cluster": "aks-prod-eastus",
        "gitlab_project_id": 1043,
        "deploy_job": "deploy-prod",
        "rancher_url": f"{RANCHER_BASE}/dashboard/c/aks-prod-eastus/explorer/apps.deployment/prod-web/ui-app",
        "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-web'))",
    },
    "auth-service": {
        "namespace": "prod-api",
        "cluster": "aks-prod-eastus",
        "gitlab_project_id": 1044,
        "deploy_job": "deploy-prod",
        "rancher_url": f"{RANCHER_BASE}/dashboard/c/aks-prod-eastus/explorer/apps.deployment/prod-api/auth-service",
        "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-api'))",
    },
    "payments-worker": {
        "namespace": "prod-workers",
        "cluster": "onprem-k8s-dc1",
        "gitlab_project_id": 1045,
        "deploy_job": "deploy-prod",
        "rancher_url": f"{RANCHER_BASE}/dashboard/c/onprem-k8s-dc1/explorer/apps.deployment/prod-workers/payments-worker",
        "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-workers'))",
    },
    "notification-service": {
        "namespace": "prod-workers",
        "cluster": "onprem-k8s-dc1",
        "gitlab_project_id": 1046,
        "deploy_job": "deploy-prod",
        "rancher_url": f"{RANCHER_BASE}/dashboard/c/onprem-k8s-dc1/explorer/apps.deployment/prod-workers/notification-service",
        "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-workers'))",
    },
    "batch-reconciler": {
        "namespace": "prod-batch",
        "cluster": "onprem-k8s-dc1",
        "gitlab_project_id": 1047,
        "deploy_job": "deploy-prod",
        "rancher_url": f"{RANCHER_BASE}/dashboard/c/onprem-k8s-dc1/explorer/apps.deployment/prod-batch/batch-reconciler",
        "kibana_url": f"{KIBANA_BASE}/app/discover#/?_a=(query:(query:'kubernetes.namespace:prod-batch'))",
    },
}

# ---------------------------------------------------- pipeline variable schema
# Rendered dynamically on the Deploy page. Keys become CI variables.

DEPLOY_PARAMS = {
    "ENV": {"type": "select", "options": ["prod", "staging", "dr"]},
    "VERSION": {"type": "text", "default": "latest"},
    "SKIP_TESTS": {"type": "bool", "default": False},
    "ROLLBACK": {"type": "bool", "default": False},
}

# ------------------------------------------------------- kubectl action allowlist
# {button label: argv template}. {ns} and {app} are substituted; nothing else
# is interpolated, so no user string ever reaches a shell.

KUBECTL_ACTIONS = {
    "Pods": ["get", "pods", "-n", "{ns}", "-l", "app={app}", "-o", "wide"],
    "Deployment": ["get", "deployment", "{app}", "-n", "{ns}", "-o", "wide"],
    "Describe": ["describe", "deployment", "{app}", "-n", "{ns}"],
    "Logs": ["logs", "-n", "{ns}", "-l", "app={app}", "--tail", "200", "--all-containers"],
    "Events": ["get", "events", "-n", "{ns}", "--sort-by", ".lastTimestamp"],
}
