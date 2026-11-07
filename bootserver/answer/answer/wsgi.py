"""gunicorn entry point: `gunicorn answer.wsgi:app`. Configured by environment; fails closed at import."""
import os
from pathlib import Path

from .app import create_app


def _make():
    inv = [Path(p) for p in os.environ["INVENTORY_FILES"].split(":") if p]
    secrets = Path(os.environ.get("SECRETS_FILE", "/run/secrets/answer_secrets/answer_secrets"))
    return create_app(inv, secrets,
                      Path(os.environ.get("STATE_PATH", "/state/state.json")), os.environ["BASE_URL"])


app = _make()
