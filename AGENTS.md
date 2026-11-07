# AGENTS.md

## Project

pve-orchenv provisions Proxmox VE 9 on bare metal. A PXE boot server (TFTP + nginx + the
answer service, one Podman pod) boots the stock installer over iPXE. The installer posts
its system info to the answer service, which matches the NIC MAC against the inventory
and returns a rendered `answer.toml` or nothing. After install, Ansible configures the
nodes: base, network (with automatic rollback), cluster create/join, storage and access.
The design is in `docs/superpowers/specs/2026-11-06-pve-bare-metal-provisioning-design.md`.

## Contract Master workflow

Automated work arrives through the Contract Master workflow. Before planning or editing,
explicitly read the issue and latest HANDOFF from Linear (project PVE Orchestration Env),
and the task's read-only Contract Master context bundle (verify its recorded source
commit and digest) with the shared workflow, security and contract guidance inside it.
Links alone do not load those files.

Claude investigates and plans; DeepSeek implements and tests; Codex independently
verifies, reviews and merges. Send results to the coordinator. Only the manager writes
Linear. Humans handle escalations. Follow the task's allowed scope, acceptance criteria
and required checks. Issues for this repository carry exactly one `repo:pve-orchenv`
label.

Registered checks (offline apart from PyPI and Ansible Galaxy, no credentials; each
builds a fresh Python 3.12 venv in a temporary directory outside the checkout):

- `test`: `pip install -r requirements-dev.txt -e bootserver/answer`, then
  `pytest -q -p no:cacheprovider bootserver/answer/tests`.
- `lint`: `pip install -r requirements-dev.txt`, then
  `ANSIBLE_CONFIG=ansible/ansible.cfg ANSIBLE_VAULT_PASSWORD_FILE=<throwaway file> ansible-lint ansible/`
  and `yamllint .`. The throwaway password only satisfies `ansible.cfg`; lint never
  decrypts the vault.

Work test-first: for each plan step, write the failing test, run it and see it fail,
make the smallest change that passes, then run the required checks. Report check output
as evidence; never claim a check passed without running it. When a test fails
unexpectedly, find the cause before changing code.

## Commands

Use Python 3.12. Run commands from the repository root.

- Dev venv: `make venv` (creates `.venv`, installs `requirements-dev.txt` and the answer
  package in editable mode).
- Tests: `make test` (pytest for `bootserver/answer/tests`).
- Lint: `make lint` (ansible-lint for `ansible/`, yamllint for all YAML). Without
  `config/vault-pass`, set `ANSIBLE_VAULT_PASSWORD_FILE` to a temporary file as the
  `lint` check does.

Operator-only targets (never run them in a task; they need podman, the vault password,
SSH keys or real hosts): `make build`, `make check`, `make bootserver-*`, `make e2e-*`
and any `ansible-playbook` run.

## Layout

- `bootserver/answer/`: Flask answer service. `inventory.py` loads and lints the
  inventory, `render.py` renders `answer.toml` and the first-boot script (disk filters
  select exactly the listed serials, otherwise `DiskMismatch`), `secrets.py` reads the
  vault, `state.py` records served hosts, `app.py` holds the routes (any doubt returns
  404, never an answer). Templates are in `answer/templates/`; tests and fixtures in
  `tests/`.
- `ansible/site.yml`: plays in order base, network, cluster (create, then join one node
  at a time), storage, access.
- `ansible/roles/`: `base`, `network`, `cluster`, `storage`.
- `ansible/tasks/`: shared task files (Proxmox API calls, VM lifecycle, diagnostics).
- `ansible/tests/`: `verify_*.yml` playbooks that check configured nodes.
- `ansible/e2e_*.yml`: nested PVE test VMs on the e2e hypervisor.
- `build/`: PXE build pipeline (`build/README.md` explains it and the boot spike).
- `bootserver/`: boot server pod definition and `bootserver.sh`.
- `inventory/`, `config/`, `versions.env`: site data, the encrypted vault and pinned
  build inputs. Operator-owned.

## Conventions

- Ansible: fully qualified module names, named tasks, idempotent changes; destructive
  steps fail closed when state is unclear. Keep ansible-lint at the `production`
  profile with no new skips.
- YAML: two-space indentation; lines up to 160 characters (`.yamllint`).
- Python: type hints, small functions, standard library first; add a test for every
  behavior change in `bootserver/answer/tests`.
- Use conventional commit subjects (`feat(ansible): …`, `fix(answer): …`). PRs explain
  behavior changes, link the Linear issue and list validation results.

## Boundaries

Always:
- Read a file before editing it and keep changes within the requested task.
- Use fictional values in tests and fixtures (example MACs, `CHANGEME-*` serials,
  made-up private IPs).

Ask first unless already authorized by the task:
- Adding dependencies or Ansible collections, changing pinned versions, adding a
  playbook, or changing lint configuration.
- Deleting files.

Never:
- Run anything that reaches real or e2e hosts, the hypervisor, the boot server or the
  network: playbooks, `make build/check/bootserver-*/e2e-*`, SSH or the Proxmox API.
- Read, decrypt, re-encrypt or edit `inventory/group_vars/all/vault.yml`; read
  `config/vault-pass`, `config/*.env` or `config/ssh/`; expose or commit secrets. This
  repository is public.
- Put real hostnames, MACs, serials, IPs or tokens from a live site into the repository.
- Push directly to `main`, force-push, or rewrite history.
