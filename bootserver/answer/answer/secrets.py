"""Decrypt the Ansible Vault file that holds the root credentials."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.errors import AnsibleError
from ansible.parsing.vault import VaultLib, VaultSecret


@dataclass
class Secrets:
    root_password_hash: str
    root_ssh_keys: list[str]


def load_secrets(vault_path: Path, password: str) -> Secrets:
    vl = VaultLib([(DEFAULT_VAULT_ID_MATCH, VaultSecret(password.encode()))])
    try:
        plain = vl.decrypt(Path(vault_path).read_bytes())
    except AnsibleError as e:
        raise ValueError(f"vault: cannot decrypt {vault_path}: {e.message}") from None
    data = yaml.safe_load(plain) or {}
    if not isinstance(data, dict):
        raise ValueError(f"vault: {vault_path}: top level must be a mapping")
    h = data.get("vault_root_password_hash")
    keys = data.get("vault_root_ssh_keys")
    if not isinstance(h, str) or not h.startswith("$"):
        raise ValueError(f"vault: {vault_path}: missing or invalid vault_root_password_hash")
    if not (isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys)):
        raise ValueError(f"vault: {vault_path}: missing or invalid vault_root_ssh_keys")
    return Secrets(root_password_hash=h, root_ssh_keys=list(keys))
