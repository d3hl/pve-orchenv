"""Root credentials for the answers.

The workstation decrypts the Ansible Vault (`load_secrets`) and writes a minimal secrets file
(`dump_secrets`) holding only what answers need: the root password hash and the root SSH public
keys. The boot server only ever sees that file (`load_secrets_file`), never the vault or its password.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Secrets:
    root_password_hash: str
    root_ssh_keys: list[str]


def _validate(h, keys, src: str, hash_key: str, keys_key: str) -> Secrets:
    if not isinstance(h, str) or not h.startswith("$"):
        raise ValueError(f"{src}: missing or invalid {hash_key}")
    if not (isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys)):
        raise ValueError(f"{src}: missing or invalid {keys_key}")
    return Secrets(root_password_hash=h, root_ssh_keys=list(keys))


def load_secrets(vault_path: Path, password: str) -> Secrets:
    """Decrypt the Ansible Vault (workstation side only)."""
    from ansible.constants import DEFAULT_VAULT_ID_MATCH
    from ansible.errors import AnsibleError
    from ansible.parsing.vault import VaultLib, VaultSecret

    vl = VaultLib([(DEFAULT_VAULT_ID_MATCH, VaultSecret(password.encode()))])
    try:
        plain = vl.decrypt(Path(vault_path).read_bytes())
    except AnsibleError as e:
        raise ValueError(f"vault: cannot decrypt {vault_path}: {e.message}") from None
    data = yaml.safe_load(plain) or {}
    if not isinstance(data, dict):
        raise ValueError(f"vault: {vault_path}: top level must be a mapping")
    return _validate(data.get("vault_root_password_hash"), data.get("vault_root_ssh_keys"),
                     f"vault: {vault_path}", "vault_root_password_hash", "vault_root_ssh_keys")


def dump_secrets(secrets: Secrets) -> str:
    return yaml.safe_dump({"root_password_hash": secrets.root_password_hash,
                           "root_ssh_keys": list(secrets.root_ssh_keys)}, sort_keys=True)


def write_secrets_file(path: Path, secrets: Secrets) -> None:
    """Write the minimal secrets file with mode 0600 (also when it already exists)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, dump_secrets(secrets).encode())
    finally:
        os.close(fd)


def load_secrets_file(path: Path) -> Secrets:
    """Read the minimal secrets file (boot server side). Raises ValueError("secrets: ...")."""
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError as e:
        raise ValueError(f"secrets: cannot read {path}: {e.strerror}") from None
    except yaml.YAMLError:
        raise ValueError(f"secrets: {path}: not valid YAML") from None
    if not isinstance(data, dict):
        raise ValueError(f"secrets: {path}: top level must be a mapping")
    extra = set(data) - {"root_password_hash", "root_ssh_keys"}
    if extra:
        # Refuse anything beyond the two values, so a vault can never be passed off as this file.
        raise ValueError(f"secrets: {path}: unexpected keys {sorted(extra)}")
    return _validate(data.get("root_password_hash"), data.get("root_ssh_keys"),
                     f"secrets: {path}", "root_password_hash", "root_ssh_keys")
