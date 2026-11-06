.PHONY: build
# Usage: make build SITE=e2e
build:
	@test -n "$(SITE)" || { echo "usage: make build SITE=<site>" >&2; exit 2; }
	SITE=$(SITE) build/build.sh

.PHONY: venv test
venv:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
	.venv/bin/pip install -e bootserver/answer

test:
	.venv/bin/pytest bootserver/answer/tests

# Lint inventory, render every host's answer into cache/answers/, validate each with
# proxmox-auto-install-assistant in the build container, and prove the disk filters
# select exactly the listed serials (needs podman, .venv and config/vault-pass).
.PHONY: check
check:
	build/check.sh
