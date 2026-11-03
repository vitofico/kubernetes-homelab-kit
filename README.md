# Kubernetes Homelab Kit

[![CI](https://github.com/vitofico/kubernetes-homelab-kit/actions/workflows/checks.yaml/badge.svg)](https://github.com/vitofico/kubernetes-homelab-kit/actions/workflows/checks.yaml)

**Your services, your Kubernetes cluster, one place to configure them.**

Build a self-hosted home for photos, books, media, files, and monitoring. Start
with a disposable Docker kind cluster, then adapt the same Kustomize applications
to your server. Keep instance settings and credentials out of Git.

**Status:** early-stage open-source template. Docker kind coverage is documented;
physical-server and external-provider integrations need operator acceptance.
See [executed tests and limits](docs/testing.md).

## Why use it?

- **Configure once.** Choose applications, domain, storage, and networking in
  `local/site.yaml`; dependencies, ingress hostnames, and TLS are derived for you.
- **Try before deploying.** Automated kind tests exercise fresh setup, HTTPS,
  persistence, NetworkPolicy, repeat deployment, and configuration changes.
- **Keep configuration private.** Settings, keys, encrypted credentials, rendered
  manifests, and diagnostics stay in ignored `local/`.
- **Start small, add services.** Nineteen optional modules use ordinary Kubernetes
  manifests and Kustomize, with explicit compatibility and image requirements.
- **Bring remote assistance.** An optional customized Hermes agent can inspect
  deployment status from chat and request bounded restarts through a separate
  broker. Kubernetes access stays read-only for the agent itself.

## Quick start

From a checkout, install Python 3.11+, Make, kubectl, Kustomize, SOPS, age, and
ShellCheck. Local integration tests also need kind and a running Docker engine
with at least 2 CPUs and 4 GiB available; larger selections need more resources.
The [CI workflow](.github/workflows/checks.yaml) records pinned tool versions and
checksum-verified installation commands. `make tools` installs Python dependencies;
it does not install the other tools.

```sh
git clone https://github.com/vitofico/kubernetes-homelab-kit.git
cd kubernetes-homelab-kit
make tools
make configure
make doctor
make plan
make test-kind
```

The wizard creates `local/site.yaml` using synthetic kind defaults. The default
selection is Traefik, cert-manager, Audiobookshelf, and Uptime Kuma. `make plan`
renders resources for inspection. `make test-kind` creates its own temporary
configuration, cluster, and kubeconfig, runs checks, then removes that cluster.
It does not deploy the configuration you just edited. Diagnostics stay local.

For repeatable setup, edit `local/site.yaml` and run
`make configure CONFIGURE_FLAGS=--non-interactive`. See the
[configuration reference](config/README.md) for inputs and secret management.

For your own independent copy, choose
[Use this template](https://github.com/vitofico/kubernetes-homelab-kit/generate).
Report reproducible bugs or suggest improvements in
[Issues](https://github.com/vitofico/kubernetes-homelab-kit/issues); use the private
reporting process in [SECURITY.md](SECURITY.md) for vulnerabilities.

## What's included

| Area | Modules |
| --- | --- |
| Routing, certificates, identity | Traefik, cert-manager, Authentik |
| Photos and files | Immich, Immich Deduper, Nextcloud |
| Books and media | Audiobookshelf, Calibre-Web, media stack, optional download integrations |
| Operations | Uptime Kuma, Argo Workflows, DNS, MetalLB, FTP camera intake |
| AI and custom services | Ollama, Hermes agent and alert router, Quire server |

The [catalog](catalog.yaml) is the source of truth for all 19 module names,
dependencies, secrets, image overrides, and kind support. Twelve modules have a
kind adapter. Seven require operator images, external credentials, hardware, or
real LAN behavior. Selection expands declared dependencies automatically;
application enrollment remains application-specific.

## Deploy to your server

Use an existing Kubernetes cluster with enforced NetworkPolicy and compatible
storage. Set `profile: server`, your domain, NFS exports, storage class, and
network addresses in `local/site.yaml`. Arrange DNS and ingress reachability;
ACME additionally needs a real domain and public HTTP challenge access. Complete
account enrollment and credential setup over a trusted LAN, VPN, or private
port-forward before allowing public access to application interfaces.

```sh
make configure
./scripts/lab secrets-edit --config local/site.yaml  # when selected modules need credentials
make doctor
make plan
# Review local/build/resources.yaml and the target kubeconfig before applying.
make up KUBECONFIG_PATH=local/kubeconfig
make status KUBECONFIG_PATH=local/kubeconfig
```

Generated passwords survive configuration runs. External credentials remain empty
until supplied through SOPS; deployment rejects missing values. Protect and back
up the age key separately. Local encryption cannot protect files from someone
who can read both key and ciphertext.

Provision the operating system, Kubernetes distribution, disks, storage services,
router, and DNS separately. Back up data before changing storage. Removing an
application from configuration does not uninstall its existing resources.

## Fix a failure while you're away

Enable optional `hermes-agent` with a compatible image and your own provider
credentials. The kit supplies a cluster-operations skill, a status helper,
persistent state, authenticated webhook wiring, and optional Telegram access
restricted to configured user IDs.

Ask the agent to inspect `audiobookshelf/audiobookshelf`, explain a failure, and
propose recovery. If you opt that exact deployment into `hermes.repair_targets`,
you can request one restart and ask it to check readiness afterward. A separate
broker accepts a fixed restart patch, limits targets, and enforces a cooldown.
Its API does not change images, storage, or arbitrary resource fields. Use
`make hermes-revoke KUBECONFIG_PATH=local/kubeconfig` before removing repair
targets; configuration changes alone do not revoke existing permissions.

This is assistance, not unattended self-healing. Chat approval is guidance to the
model; anyone controlling the agent and its token can request allowed restarts.
Status and chat data may reach your model provider. Image requirements, setup,
permissions, commands, and remaining risks are in the [Hermes guide](docs/hermes.md).
Real provider and chat delivery require operator acceptance testing.

## Develop and contribute

```sh
make lint test-unit privacy
make validate-all VALIDATE_FLAGS=--prepare  # first run fetches pinned tools/schemas
make validate-all                          # later schema validation is offline
make test-kind-all                         # all kind-compatible modules
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) for contribution checks and
[SECURITY.md](SECURITY.md) for private reporting and publication boundaries.
The [testing guide](docs/testing.md) distinguishes schema checks, runtime tests,
and external integrations. The [release checklist](docs/release-readiness.md)
describes review before publication.

Original tooling and documentation use the [MIT license](LICENSE). Bundled
upstream definitions retain their licenses, including Nextcloud-derived AGPL-3.0
material; see [third-party notices](THIRD_PARTY_NOTICES.md).
