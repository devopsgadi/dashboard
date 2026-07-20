"""
LLM client — talks to an internal / self-hosted endpoint.

Configured entirely from credentials.json under the "llm" key so no endpoint or
key is hardcoded. Supports the two shapes most internal gateways expose:
  - "openai"    : POST {base}/chat/completions   (vLLM, TGI, LiteLLM, Azure OpenAI)
  - "anthropic" : POST {base}/v1/messages

Mock mode returns a plausible diagnosis so the whole flow works with no endpoint.

  "llm": {
    "provider": "openai",
    "base_url": "https://llm.internal.corp/v1",
    "api_key": "sk-internal-...",
    "model": "llama-3.3-70b-instruct",
    "timeout": 60
  }
"""

import json

import credentials as creds
from config import USE_MOCK

_cfg = creds.get("llm", default={}) or {}
PROVIDER = _cfg.get("provider", "openai")
BASE_URL = (_cfg.get("base_url") or "").rstrip("/")
API_KEY = _cfg.get("api_key", "")
MODEL = _cfg.get("model", "internal-model")
TIMEOUT = _cfg.get("timeout", 60)

SYSTEM = (
    "You are a senior SRE assistant embedded in a deployment dashboard. "
    "You explain Kubernetes and Helm failures from read-only evidence and "
    "suggest fixes for a human to apply. You never claim to have taken action."
)


def configured() -> bool:
    return bool(BASE_URL and API_KEY) and not USE_MOCK


def complete(prompt: str) -> dict:
    """Return {ok, text, error, model}. Never raises."""
    if USE_MOCK or not BASE_URL:
        return {"ok": True, "text": _mock_response(prompt),
                "error": None, "model": "mock"}

    try:
        import requests
    except ImportError:
        return {"ok": False, "text": "", "error": "requests not installed", "model": MODEL}

    try:
        if PROVIDER == "anthropic":
            return _anthropic(requests, prompt)
        return _openai(requests, prompt)
    except Exception as e:
        return {"ok": False, "text": "", "error": str(e), "model": MODEL}


def _openai(requests, prompt):
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 900,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return {"ok": True, "model": MODEL, "error": None,
            "text": data["choices"][0]["message"]["content"].strip()}


def _anthropic(requests, prompt):
    r = requests.post(
        f"{BASE_URL}/v1/messages",
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 900,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"ok": True, "model": MODEL, "error": None, "text": text.strip()}


def _mock_response(prompt: str) -> str:
    """Pattern-matches the evidence in the prompt to a canned-but-plausible answer."""
    p = prompt.lower()
    if "imagepullbackoff" in p:
        return (
            "**Root cause:** The image tag being pulled does not exist in the registry — "
            "the deployment is pinned to a tag that was never pushed (likely a wrong VERSION "
            "in the deploy parameters).\n\n"
            "**Remediation:**\n"
            "1. Confirm the intended tag: `docker manifest inspect registry.example.com/api-service:<tag>`.\n"
            "2. If the tag is wrong, re-deploy with the correct VERSION from the Deploy page.\n"
            "3. If the tag is right but private, verify the imagePullSecret exists and is referenced.\n"
            "4. Once fixed, the ImagePullBackOff clears on the next pull retry automatically."
        )
    if "crashloopbackoff" in p or "api_base_url" in p:
        return (
            "**Root cause:** The container starts and immediately exits because a required "
            "config value (`API_BASE_URL`) is empty. This is a configuration gap, not an "
            "image or scheduling problem.\n\n"
            "**Remediation:**\n"
            "1. Set `API_BASE_URL` in the app's configmap/secret (Actions → Update secret if it's a secret).\n"
            "2. Roll the deployment so pods pick up the value.\n"
            "3. Watch the log tail — the FATAL line should be gone on restart.\n"
            "The restart count will keep climbing until the missing value is supplied."
        )
    if "not found" in p and "secret" in p:
        return (
            "**Root cause:** The deployment references a secret that doesn't exist in the "
            "namespace, and the Helm upgrade's post-upgrade hooks failed as a result — the "
            "release is now in a `failed` state.\n\n"
            "**Remediation:**\n"
            "1. Create the missing secret (Actions → Update secret creates the key).\n"
            "2. Because the Helm release is `failed`, roll back to the last good revision "
            "(Actions → Rollback) or re-run the upgrade once the secret exists.\n"
            "3. Verify with `helm status` that the release returns to `deployed`."
        )
    return (
        "**Assessment:** The evidence doesn't point to a single unambiguous cause. "
        "Collect the previous container's logs (`--previous`) and a full `describe pod` "
        "for the affected pod, then re-run diagnosis."
    )
