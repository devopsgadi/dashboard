"""
Channels — per-project/channel parameter sets.

A "channel" is a named project whose deploy parameters are fixed and apply to
every app in an uploaded Excel. You pick one channel in the UI; its JSON params
get passed to all rows.

Files live in params/<channel>.json, e.g.:

  params/digital-banking.json
  {
    "ENV": "prod",
    "SKIP_TESTS": false,
    "ROLLBACK": false,
    "DEPLOY_STRATEGY": "rolling",
    "NOTIFY_CHANNEL": "#deploys-banking"
  }

VERSION is intentionally not set here — it comes per-row from the Excel.
"""

import json
from pathlib import Path

PARAMS_DIR = Path(__file__).parent / "params"


def available_channels() -> list[str]:
    """Channel names from params/*.json. Falls back to a built-in demo set."""
    if PARAMS_DIR.exists():
        names = sorted(p.stem for p in PARAMS_DIR.glob("*.json"))
        if names:
            return names
    return list(_DEMO.keys())


def load_params(channel: str) -> dict:
    path = PARAMS_DIR / f"{channel}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            return {"_error": f"invalid JSON in {path.name}: {e}"}
    return dict(_DEMO.get(channel, {}))


# Built-in demo channels so the UI works before params/ exists.
_DEMO = {
    "digital-banking": {
        "ENV": "prod", "SKIP_TESTS": False, "ROLLBACK": False,
        "DEPLOY_STRATEGY": "rolling", "NOTIFY_CHANNEL": "#deploys-banking",
    },
    "internal-tools": {
        "ENV": "staging", "SKIP_TESTS": True, "ROLLBACK": False,
        "DEPLOY_STRATEGY": "recreate", "NOTIFY_CHANNEL": "#deploys-internal",
    },
}
