# Homelab Kit

A configurable Kubernetes homelab template with a Docker-backed kind profile for local trials and a separate server profile. Application definitions use Kustomize; instance settings belong in an ignored local workspace.

This repository is a local review candidate. Review [release readiness](docs/release-readiness.md) and [test coverage](docs/testing.md) before deploying or publishing it.

## Prerequisites

Use Python 3.11 or later, Make, kubectl, Kustomize, SOPS, age, and ShellCheck. Local integration tests also require Docker, kind, and curl. `make tools` installs the pinned Python dependency into `.venv`; `make doctor` checks the remaining tools and configuration. The CI workflow records tested tool versions and verified download commands.

## Configure once

```sh
make tools
make configure
make doctor
make plan
```

The setup wizard writes `local/site.yaml`. This is the one file to edit for your domain, application selection, storage, and network settings. Generated passwords and supplied integration credentials are encrypted in a companion file; generated keys and rendered output stay in the ignored local workspace. Keep a secure backup of your key and credentials.

Use `make configure CONFIG=local/site.yaml` for subsequent setup, or add `CONFIGURE_FLAGS=--non-interactive` for automation. See [configuration](config/README.md) for the exact schema, noninteractive options, and required tools. Optional modules may require an image override or external credentials; the catalog records these requirements.

## Try it locally

Start Docker, then run:

```sh
make test-kind
```

Use `make test-kind-all` to add sequential rollout checks for the remaining kind-compatible catalog modules.

The test harness uses a dedicated cluster and kubeconfig. It exercises the shared configuration and application rendering flow. See [test coverage](docs/testing.md) for tested applications, assertions, and platform limitations.

## Deploy to a server

Configure the server profile and provide your own domain, storage, and network settings. Review the rendered plan before applying. Deploy with `make up KUBECONFIG_PATH=local/kubeconfig` after supplying that explicit kubeconfig. See `./scripts/lab --help` and command-specific help for usage. The server profile targets an existing Kubernetes cluster. Host provisioning, disk formatting, router configuration, and DNS-provider accounts require separate operator setup.

Application data is persistent. Back it up before changing storage configuration or removing applications. Test-environment cleanup and production-data removal are separate operations.

## Privacy and contribution checks

```sh
python3 tools/audit-publication.py
make validate
make validate-all VALIDATE_FLAGS=--prepare
make lint
```

The first `validate-all` run prepares pinned tools and schemas; later `make validate-all` runs offline. See [schema validation](tools/validate-catalog.md).

Run an offline secret scanner over the eligible files and Git history before publication. Do not commit local settings, generated credentials, rendered manifests, screenshots, operational inventories, or deployment logs. A clean automated scan is one input to a manual privacy review.

The application catalog includes explicit exclusions and compatibility limits. External providers and hardware-specific behavior require separate acceptance tests.
