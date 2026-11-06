"""Persistent per-host install state (new -> served -> installed), written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"state file {self.path} is corrupt: {e}") from None
        if not isinstance(data, dict):
            raise ValueError(f"state file {self.path} is corrupt: not a mapping")
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def all(self) -> dict[str, dict]:
        return self._load()

    def get(self, host: str) -> dict:
        rec = self._load().get(host)
        if not isinstance(rec, dict) or rec.get("state") not in ("new", "served", "installed"):
            return {"state": "new", "last_seen": None}
        return rec

    def _set(self, host: str, state: str) -> None:
        data = self._load()
        data[host] = {"state": state, "last_seen": _now()}
        self._save(data)

    def mark_served(self, host: str) -> None:
        self._set(host, "served")

    def mark_installed(self, host: str) -> None:
        self._set(host, "installed")

    def reset(self, host: str) -> None:
        self._set(host, "new")
