# Scheduled dynamic DNS updates

The optional `dynamic-dns-updater` module runs the companion
[Dynamic DNS Updater](https://github.com/vitofico/dynamic-dns-updater) as a
one-shot Kubernetes CronJob. Its image repository is
`ghcr.io/vitofico/dynamic-dns-updater/updater`. It supports DuckDNS, deSEC/dedyn,
FreeDNS, and Dynu. The module is separate from the LAN `dns` application and
requires no Argo installation.

The catalog pins the reviewed companion image by immutable digest. Its OCI
index includes `linux/amd64` and `linux/arm64` variants. Use
`images.IMAGE_DYNAMIC_DNS_UPDATER` to select a different reviewed release or a
compatible alternative. The pinned image is currently private while public
publication is being prepared; deployment needs registry access until package
visibility changes. Verify an anonymous pull after publication. Existing provider
credentials must never be built into an image.

## Configure locally

Add `dynamic-dns-updater` to the applications in ignored `local/site.yaml`,
retaining your other configuration. Use the catalog image or the override above and:

```yaml
ddns:
  provider: desec
  domains: host.example.net,other.example.net
  schedule: '*/15 * * * *'
  suspend: true
  ipv4: auto
  ipv6: preserve
```

Replace the synthetic hostnames with domains you control. Use the server
profile; kind tests never contact real DNS providers. The job starts suspended,
including when this section is omitted. It has no Kubernetes API token or write
permissions, runs as a non-root user with a read-only root filesystem, and has a
120-second execution deadline. Outbound DNS and public HTTPS connectivity are
required.

`make configure` preserves existing credentials and adds an empty external
credential field. Run `./scripts/lab secrets-edit --config local/site.yaml` and
supply the selected key beneath
`dynamic-dns-updater.dynamic-dns-credentials`:

| Provider | Non-secret fields in `ddns` | Encrypted credential key |
|---|---|---|
| `duckdns` | `domains`: comma-separated DuckDNS labels | `DUCKDNS_TOKEN` |
| `desec` | `domains`: comma-separated full hostnames in one domain; `ipv4`, `ipv6` | `DESEC_TOKEN` |
| `freedns` | none | `FREEDNS_TOKEN`: one token or a comma-separated list |
| `dynu` | exactly one of `hostname` or `group` | `DYNU_PASSWORD`: provider update credential |

The encrypted store is local. Rendering emits Secret references, never credential
values. `doctor` and deployment reject missing credentials, even for a suspended
job. Changing the provider requires running configure again to add its new
credential key. Other fields from an earlier provider remain inert.

The updater defaults to IPv4 autodetection while preserving IPv6 for deSEC.
Each family accepts `auto`, `preserve`, or an explicit address; at most one
family may use autodetection. This avoids accidentally deleting an existing
record for the other address family. See the
[official deSEC update contract](https://desec.readthedocs.io/en/latest/dyndns/update-api.html).

## Enable deliberately

Run `make doctor`, review `make plan`, then set `ddns.suspend: false` and apply
with the explicit target kubeconfig. Confirm one scheduled Job succeeds and
verify the intended records independently. The updater does one update and
exits; it contains no scheduler. CronJob overlap is forbidden and failed Jobs
get one retry. Consult the provider's rate limits before shortening the schedule.

To stop future updates, set `ddns.suspend: true` and apply that change before
removing the module. Removing an application from configuration does not remove
an existing CronJob, and suspending it does not cancel a currently running Job.
Revoke the provider token when retiring the integration.

## Migration from the older image

Replace `ghcr.io/vitofico/docker-duckdns-updater` with the reviewed release digest
from `ghcr.io/vitofico/dynamic-dns-updater/updater`. Use the default command or
`/usr/local/bin/update_dns.sh`; the old `update_duckdns.sh` path is obsolete.
Earlier `ghcr.io/vitofico/dynamic-dns-updater` flat packages are also legacy
packages. Retain existing private images until the replacement is verified; the
fresh source repository and nested package have separate publication settings.
Provider variables retain their documented names. deSEC updating now uses the
same companion updater as DuckDNS rather than a copied shell implementation.
