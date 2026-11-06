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

# Boot server pod (TFTP + HTTP + answer service). Usage: make bootserver-up SITE=e2e
# Rebuilt dist/<site>? Run bootserver-up again (it mounts the resolved build dir).
.PHONY: bootserver-up bootserver-down bootserver-deploy
bootserver-up bootserver-down bootserver-deploy:
	@test -n "$(SITE)" || { echo "usage: make $@ SITE=<site>" >&2; exit 2; }
	bootserver/bootserver.sh $(patsubst bootserver-%,%,$@) $(SITE)

# Lint the Ansible code and all YAML.
.PHONY: lint
lint:
	ANSIBLE_CONFIG=ansible/ansible.cfg .venv/bin/ansible-lint ansible/ && .venv/bin/yamllint .

# Nested PVE test nodes (VMIDs 9901-9903) on the e2e hypervisor. KEEP=1 skips the teardown.
.PHONY: e2e-up e2e-down
e2e-up:
	ANSIBLE_CONFIG=ansible/ansible.cfg .venv/bin/ansible-playbook ansible/e2e_up.yml -i inventory/e2e.yml

e2e-down:
	@if [ -n "$(KEEP)" ]; then echo "KEEP set: leaving the e2e VMs in place"; else \
	ANSIBLE_CONFIG=ansible/ansible.cfg .venv/bin/ansible-playbook ansible/e2e_down.yml -i inventory/e2e.yml; fi
