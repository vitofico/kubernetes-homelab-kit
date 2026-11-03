# Testing and coverage

Run `make tools`, then `make test-unit` and `make test-kind` from a fresh checkout.
Use `make test-kind-all` for the broader catalog runtime suite.
The integration suite needs a running Docker engine, kind, kubectl, SOPS, age,
Kustomize, curl, and sufficient free Docker resources. Images are pulled from
public registries; neither production credentials nor a physical server are used.

The kind suite creates a dedicated two-node cluster with an isolated kubeconfig.
It refuses to reuse an existing cluster with the same name. It removes only the
cluster it created, on both success and failure by default. For debugging,
`KEEP_KIND_ON_FAILURE=1 make test-kind-all` retains only the owned test cluster
after a failure; remove it explicitly with the printed cleanup command.
Private diagnostics remain in
ignored `local/test-work/run.*` directories. The default cluster name can be changed
with `TEST_CLUSTER_NAME`; no current kubeconfig context is switched.

The pinned Kubernetes node image and checksum-verified Calico installation provide
an environment where NetworkPolicy is enforced. The cluster setup follows the
[official Calico kind guide](https://docs.tigera.io/calico/latest/getting-started/kubernetes/kind).

The CLI uses a conservative rollout wait derived from declared startup probes,
with a five-minute floor and two-minute readiness allowance. Kubernetes controller
progress-deadline errors remain failures. Each broader catalog case has a bounded
30-minute ceiling; this does not guarantee readiness on an undersized cluster.

The default suite exercises the normal configuration and deployment commands with
Traefik, cert-manager, Audiobookshelf, and Uptime Kuma:

- Fresh setup and workload readiness.
- Application service HTTP responses.
- HTTPS through Traefik using the generated local CA, including hostname validation.
- Data retained across pod recreation.
- NetworkPolicy permitting the ingress namespace and denying an unrelated client.
- Repeated deployment and deterministic rendered output.
- Domain changes applied to application ingresses.
- Scoped cluster teardown, including failure paths.

A separate fixture uses an inert Python process in place of the external Hermes
image while retaining its real initialization, volumes, identity, and policies.
Five checks verify nonroot state/skill setup; read-only agent and named broker
RBAC; actual API status and fixed restart with auth/body/target/cooldown rejection;
NetworkPolicy denial for an unrelated client; and explicit revocation preserving
the agent and application. No real model or chat service is contacted.

Run `make validate-all VALIDATE_FLAGS=--prepare` once to prepare the pinned validator
and schemas, then `make validate-all` validates offline. This covers all 19
application bases against Kubernetes 1.37 and embedded CRD schemas; unknown
schemas and invalid objects fail the check. Unit tests also render each isolated
module with dependency expansion and check all Ingress backend Services/ports. No schema skips are accepted. `kind_supported` in the catalog
means that an application has a kind adapter, not that every external integration
or application feature has been certified. Modules requiring operator-provided
images, physical camera connectivity, LAN DNS, or bare-metal load-balancer pools
cannot be reproduced by the default kind suite. Test credentials are generated
locally and excluded from the publication snapshot.

## Verification record

On 2026-11-03 the fresh default kind profile and synthetic Hermes fixture passed
all fifteen assertions,
including certificate trust, hostname checks, same-host HTTPS redirects,
persistence, network-policy enforcement, repeated setup, and domain changes.
The environment used kind 0.33.0, Kubernetes 1.37.0, Calico 3.33.0, and Docker
29.9.2 with Linux/arm64 nodes.

The final configuration, broker, revocation, routing, and safety unit run passed 72 tests.

The strict schema pass validated 330 objects across all 19 modules with zero
invalid objects, errors, or skipped schemas. Negative checks confirmed that an
invalid native object, invalid custom resource, and missing schema fail.

All eight additional kind-compatible modules passed after the fresh-install
issues found by the first pass were corrected and the affected cases retested.
The default four modules plus this matrix cover all 12 kind-compatible modules.

| Additional module | Runtime result | HTTPS route checks |
| --- | --- | --- |
| Argo | Ready | No Ingress; readiness only |
| Authentik | Passed from fresh database claims | 1 |
| Calibre-Web | Passed | 1 |
| Immich Deduper, with Immich dependency | Passed | 2 |
| Immich | Passed | 1 |
| Media stack: Jellyfin, Prowlarr, Radarr, Sonarr, Transmission, Flaresolverr | All six workloads ready | 5 |
| Nextcloud, with MariaDB and Redis | Passed from empty synthetic data | 2 declared route entries |
| Ollama | Ready | No Ingress; readiness only |

The media applications use their own hostnames and upstream default root paths;
Transmission's native redirect stays on its dedicated hostname. Original failures
and successful rechecks remain in the ignored local test work directory. The
current-source baseline Ingress names and TLS settings are also checked against
the running resources before the test cluster is removed.

Seven modules are schema-validated but excluded from kind runtime certification:
`ad-alta-voce-dl`, `hermes-agent`, `hermes-alert-router`, and
`quire-server` require operator-supplied compatible images or external credentials;
`ftp-camera` requires physical LAN connectivity; `dns` and `metallb` require real
network/provider integration. Their catalog entries state the requirements.

These are deployment, persistence, network-policy, and routing tests, rather than
complete third-party application feature tests. The server profile is schema-
validated; real NFS exports, storage ownership, external providers, and hardware
still require operator verification. No live server deployment was performed.

The full catalog first pass found a short Authentik rollout wait and optional
Quire routes declared by Calibre-Web. After fixing them, fresh targeted rechecks
passed both modules. The combined result covers all 12 kind-compatible modules;
the first failing run and successful rechecks remain distinct in private logs.

Final teardown used the explicit test kubeconfig. Verification confirmed that only
the owned test cluster was removed and the unrelated cluster inventory was
preserved. No live server deployment or global Docker cleanup was performed.
