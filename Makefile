SHELL := /bin/bash
LAB := ./scripts/lab
CONFIG ?= local/site.yaml
OUTPUT ?= local/build
KUBECONFIG_PATH ?=
CONFIGURE_FLAGS ?=
.PHONY: help tools configure doctor render plan up status validate lint privacy test test-unit test-kind
help:
	@echo 'make tools configure doctor plan up status validate validate-all test-unit test-kind'
	@echo 'CONFIG=local/site.yaml KUBECONFIG_PATH=/explicit/path OUTPUT=local/build'
tools:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
configure:
	$(LAB) configure --config "$(CONFIG)" $(CONFIGURE_FLAGS)
doctor:
	$(LAB) doctor --config "$(CONFIG)"
render:
	$(LAB) render --config "$(CONFIG)" --output "$(OUTPUT)"
plan:
	$(LAB) plan --config "$(CONFIG)" --output "$(OUTPUT)"
up:
	$(LAB) up --config "$(CONFIG)" --kubeconfig "$(KUBECONFIG_PATH)"
status:
	$(LAB) status --config "$(CONFIG)" --kubeconfig "$(KUBECONFIG_PATH)"
validate:
	$(LAB) validate --config "$(CONFIG)"
lint:
	$(LAB) lint
privacy:
	python3 tools/audit-publication.py
test-unit:
	$(LAB) test-unit
test: test-unit
test-kind:
	./tests/test-kind.sh

.PHONY: test-kind-all
test-kind-all:
	KIND_TEST_EXTRA_SCRIPT="$(CURDIR)/tools/test-catalog-hook.sh" ./tests/test-kind.sh

.PHONY: hermes-revoke
hermes-revoke:
	./scripts/revoke-hermes.sh "$(KUBECONFIG_PATH)"

VALIDATE_FLAGS ?=
.PHONY: validate-all
validate-all:
	./tools/validate-catalog.sh $(VALIDATE_FLAGS)
