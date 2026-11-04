# Instance configuration

Run `make tools`, then `make configure`. The only instance settings file is
`local/site.yaml`; the wizard creates it from `site.example.yaml`. All local
configuration, generated resources, kubeconfigs, credentials, and diagnostics
belong in ignored `local/`. Commands reject configuration/output writes into
application bases. Do not add `local/` to version control.

The configuration validator rejects unknown keys, malformed domains and IPs,
unsupported profiles, duplicate or unknown applications, unresolved template
markers, and unavailable required image overrides. Application dependencies are
expanded and ordered automatically. The catalog records modules that require
physical hardware or external integrations and cannot run in kind.

| Field | Meaning |
|---|---|
| `profile` | `kind` for the disposable two-node Docker cluster; `server` for an existing Kubernetes cluster |
| `cluster_name` | kind cluster name (its kubeconfig context must be `kind-<name>`) |
| `domain` | Base domain used by application ingress rules |
| `tls.mode` | `selfsigned` creates a local CA and issuer; `acme` requests public certificates on the server profile |
| `tls.email` | ACME registration contact; required only for `acme` |
| `applications` | Enabled catalog application names |
| `storage.nfs_server` | NFS server IP for server-profile static volumes |
| `storage.nfs_root` | NFS export root for server-profile static volumes |
| `storage.class_name` | Optional dynamic storage class for the server profile (default `microk8s-hostpath`); set e.g. `local-path` for a compatible existing provisioner. Kind always uses `standard`. Static NFS classes remain unchanged. |
| `network.lan_cidr` | Permitted LAN range for applicable network policies |
| `network.loadbalancer_ip` | Pins the server-profile Traefik LoadBalancer address and supplies the matching DNS address; other LoadBalancer services use automatic pool allocation |
| `network.loadbalancer_pool` | Optional ordered `start-end` range for MetalLB, inside the LAN, containing `loadbalancer_ip`, excluding the NFS server |
| `network.ftp_ip` | Required for server `ftp-camera`: distinct pool address used for both its Service and passive FTP advertisement |
| `hermes` | Optional API endpoint CIDRs, named restart targets, and Telegram access settings; see [Hermes setup](../docs/hermes.md) |
| `ddns` | Optional provider, schedule, suspension and hostname/IP settings for the [dynamic DNS updater](../docs/dynamic-dns.md) |
| `images` | Override catalog image defaults or supply `required_images` without defaults; keys may include surrounding double underscores or omit them |

`configure --non-interactive` accepts the checked-in kind defaults. For automation,
prepare `local/site.yaml` and run:

```sh
./scripts/lab configure --non-interactive --config local/site.yaml
./scripts/lab doctor --config local/site.yaml
./scripts/lab plan --config local/site.yaml --output local/build
./scripts/lab up --config local/site.yaml --kubeconfig local/kubeconfig
```

The kind profile changes MicroK8s dynamic storage classes to kind's `standard`
class, converts static NFS volumes to node-local directories on the worker, and
converts LoadBalancer services to ClusterIP. It uses the same application bases
and local self-signed TLS issuer; it does not reproduce server NFS or public DNS.
Deleting a kind node also deletes its local volumes. Pod restarts preserve them.

Application configuration directories beneath `storage.nfs_root` are converted
into separate per-workload ReadWriteOnce PVCs, rather than network filesystems
for SQLite databases. Their names combine the workload and volume names.
`storage.config_size` sets capacity (default `10Gi`); the server uses
`storage.class_name` and kind uses `standard`. Mount subdirectories and read-only
settings are preserved within each workload. Choose a database-compatible storage
class. Media libraries and other existing NFS PVs continue to use NFS; device,
socket, and unrelated host mounts are not replaced.

The kind profile prepares Nextcloud's mapped data-volume root and its immediate
mount subdirectories for the pod's configured filesystem group before startup.
It changes group permissions on those directories only. For the server profile,
prepare NFS export permissions yourself to match the workload security context
(Nextcloud uses group `33`); existing files and server exports retain their
operator-managed ownership.

## Local secrets

For modules that need credentials, configure generates an age key and
`local/secrets.sops.yaml`, both with mode `0600`. The key is colocated with the
encrypted file for the local development workflow; encryption is not protection
against someone who can read both. Back up your key separately and protect this
directory. Missing keys are never silently replaced when ciphertext exists.
Repeated configure runs preserve existing credentials.

Passwords and internal shared secrets are generated. External provider credentials
are left empty; `doctor` and `up` reject missing values. Supply them using:

```sh
./scripts/lab secrets-edit --config local/site.yaml
```

This invokes SOPS with the correct local age key. Rendering and `plan` exclude
Secret objects; `up` sends decrypted credentials to the explicitly selected
cluster through stdin without writing a plaintext manifest. Private diagnostics
are stored under `local/diagnostics`; Secret-apply errors are withheld to prevent
kubectl from echoing credential values into logs.

The opt-in Nextcloud module currently includes SMTP environment configuration.
Provide its `smtp-host`, `smtp-username`, and `smtp-password` through the encrypted
store before deploying it. They are external inputs and are deliberately left
empty, alongside VPN and AI-provider credentials. Internal database passwords,
shared webhook keys, and the Authentik bootstrap token are generated locally.
Unknown future secret fields also remain empty until an operator supplies them.

The server profile requires an existing configured cluster and operator-managed
NFS exports, network-policy enforcement, DNS, and compatible storage. It does
not partition disks, install a Kubernetes distribution, modify router settings,
or publish DNS records unless you explicitly enable the optional dynamic DNS job.

## Media application routes

Enabling `k8s-mediaserver` derives separate HTTPS hostnames from `domain`:
`prowlarr.<domain>`, `radarr.<domain>`, `sonarr.<domain>`,
`transmission.<domain>`, and `jellyfin.<domain>`. Each ingress forwards `/` and
its child paths to that application. No additional hostname inputs or seeded
application configuration are needed on a fresh installation. Transmission
keeps its native redirect and web/RPC paths beneath its dedicated hostname.

When reusing existing Servarr application data configured with a nonempty
`UrlBase`, reset that setting to an empty value before moving to these dedicated
hostnames. Existing application configuration is preserved rather than silently
rewritten by deployment.
