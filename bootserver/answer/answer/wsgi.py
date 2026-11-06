"""gunicorn entry point: `gunicorn answer.wsgi:app`. Configured by environment; fails closed at import."""
import os
from pathlib import Path

from .app import create_app


def _make():
    inv = [Path(p) for p in os.environ["INVENTORY_FILES"].split(":") if p]
    password = Path(os.environ.get("VAULT_PASSWORD_FILE", "/run/secrets/vault_password")).read_text().rstrip("\n")
    return create_app(inv, Path(os.environ["VAULT_PATH"]), password,
                      Path(os.environ.get("STATE_PATH", "/state/state.json")), os.environ["BASE_URL"])


app = _make()
