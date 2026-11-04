# Remote cluster assistance with Hermes

The optional `hermes-agent` integration customizes
[Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent) with
a cluster-operations skill and Python helpers. It supports inspection and an
opt-in restart workflow from an authenticated chat.

## Image and platform prerequisites

The companion [Hermes container repository](https://github.com/vitofico/hermes-agent)
builds `ghcr.io/vitofico/hermes-agent` and
`ghcr.io/vitofico/hermes-agent-router`. The catalog pins reviewed companion
builds by immutable digest. Override `images.IMAGE_HERMES_AGENT` and, when
enabling the router, `images.IMAGE_HERMES_ALERT_ROUTER` to select a different
reviewed build. Companion images remain private while publication is prepared;
your cluster needs registry access until package visibility changes. Verify
anonymous image pulls after publication. The companion gateway and router images
currently target
`linux/amd64`; use independently validated alternative images on other
architectures. Compatible alternatives remain supported through these overrides.
The image must start the Hermes messaging gateway, support
the configured Anthropic model and Telegram adapter, contain Python 3 with TLS
and a POSIX shell, and run correctly as UID/GID `10000` with dropped capabilities.
It must honor `HOME`, `HERMES_HOME`, and `HERMES_WRITE_SAFE_ROOT`, load skills from
`$HERMES_HOME/skills`, and expose the webhook health endpoint on port `8644`.

The webhook route deliberately sets `toolsets: []`. A compatible image must
preserve that explicit empty list through both the webhook adapter and the turn
tool resolver, including default MCP tools. Some upstream versions interpret an
empty list as a request for default tools. The companion build patches and tests
this behavior for its pinned base; independently verify this contract before
selecting another image or upgrading the base.

An image name alone does not establish compatibility. Some upstream container
entrypoints require root for supervision; adapt the entrypoint to run the gateway
as this UID and verify it before selecting that image. The companion repository
owns image releases; this kit runs no install script during deployment.

The kit mounts `/opt/hermes-ops/ops.py`, supplies the `cluster-ops` skill, and copies
managed configuration into the persistent Hermes home at startup. Persistent
state may contain private conversations. The initializer prepares only the volume
root and `.hermes` directory ownership; it does not recursively modify existing
state. Kubernetes may adjust group ownership according to the storage driver's
`fsGroup` behavior. Root-squashed exports need operator-prepared permissions for
UID/GID `10000`.

Use the server profile and a CNI that enforces NetworkPolicy. Determine the exact
Kubernetes Service IP and API endpoint IP(s) visible to the CNI. Supply individual
`/32` IPv4 or `/128` IPv6 CIDRs. Service translation can occur before or after
policy enforcement depending on the CNI. Verify API access on your own cluster.
Set `hermes.api_ports` to include both the Service and endpoint ports; defaults
are `443` and `6443`. MicroK8s commonly needs `16443` for its endpoint. Provider
egress permits public HTTPS, not a provider-domain allowlist. Translation prefixes
are blocked; DNS64/NAT64-only networks require a separately reviewed egress policy.

## Configure the integration

Merge these fields into ignored `local/site.yaml`, retaining other required
configuration. These settings use the pinned catalog image; the addresses and
account identifier are synthetic examples, not usable credentials:

```yaml
applications:
  - audiobookshelf
  - hermes-agent
hermes:
  api_cidrs:
    - 192.0.2.1/32 # replace with each API address visible to your CNI
  api_ports: [443, 6443] # include the API endpoint port; MicroK8s commonly uses 16443
  repair_targets: [] # fresh setup: no broker or write permissions
  telegram:
    enabled: true
    allowed_users:
      - '123456789' # replace with your numeric Telegram user ID
```

Run `make configure` to add encrypted secret entries without replacing existing
values, then `./scripts/lab secrets-edit --config local/site.yaml`. Supply
`ai-provider-keys.anthropic` and, when Telegram is enabled,
`hermes-agent-secrets.TELEGRAM_BOT_TOKEN`. Shared webhook and repair tokens are
generated locally. `make doctor` rejects missing required credentials.

Telegram uses upstream `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS`. See the
[official Telegram guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
for bot creation and account IDs. Verify an unlisted account is rejected. Use a
private conversation and do not turn on open access. This kit does not open a
public inbound chat endpoint.

Run `make plan` and inspect the resources. Deploy with the explicit target
kubeconfig, then verify readiness, loaded skill, status reads, and account
restrictions before enabling repairs.

## Opt in to one bounded repair

Replace the empty target list, retaining the other Hermes settings:

```yaml
hermes:
  repair_targets:
    - namespace: audiobookshelf
      deployment: audiobookshelf
```

Targets must be deployments belonging to enabled application modules. The `agent` and `kube-system`
namespaces cannot be targets. Avoid security-sensitive controllers in other namespaces. Re-run configuration, review the plan, and
deploy. This adds a separate stock-Python broker, its own service account,
narrowly named Roles, and a shared token. Without targets these resources and the
token requirement are absent.

The installed helper provides:

```sh
python3 /opt/hermes-ops/ops.py status audiobookshelf audiobookshelf
python3 /opt/hermes-ops/ops.py restart audiobookshelf audiobookshelf
```

Ask the agent to inspect the deployment and propose a restart first. Once you
authorize it, the skill requests one restart and checks status again. A successful
patch is not proof of recovery. If readiness remains low, investigate the cause
instead of repeatedly restarting it.

## Disable or reduce repair authority

**Changing configuration does not delete resources already deployed.** Before
removing targets, disabling the agent, or revoking access, run:

```sh
make hermes-revoke KUBECONFIG_PATH=local/kubeconfig
```

This explicit command revokes labeled broker RoleBindings first, then its Roles,
Deployment, Service, service account, policies, and Kubernetes token Secret. It
preserves applications, agent state, and local encrypted credentials. It needs
permission to list labeled RBAC resources across namespaces; any failed operation
stops the command instead of reporting success. Inspect and resolve partial
failures before treating access as revoked.

Then update `hermes.repair_targets` and apply the new configuration. Empty targets
omit broker resources on a fresh deployment, but do not revoke an older broker by
themselves. Before re-enabling, replace `hermes-repair-credentials.REPAIR_TOKEN` in
the local encrypted store with a new high-entropy value using SOPS; configuration
preserves existing values. Previously copied tokens otherwise become valid again.
Keep old diagnostic kubeconfigs and manifests private.

## Enforced boundary and remaining risks

- The agent can read workload metadata/status and events across the cluster. It
  cannot read Kubernetes Secrets, execute commands in pods, or patch workloads.
  Read access can still reveal sensitive operational details.
- The broker requires a bearer token and an exact allowlisted namespace and
  deployment. It rejects extra fields and constructs only a timestamp annotation
  patch on the deployment's pod template, with a 60-second per-target cooldown.
  Cooldown state is in memory and resets when the broker restarts; it is not a
  durable rate limit.
- Broker RBAC permits patching only the named deployments. Kubernetes RBAC cannot
  restrict patch fields: a compromised broker could make other patches on those
  deployments. The fixed code is part of the boundary.
- The broker is cluster-internal, restricted by NetworkPolicy, and uses verified
  HTTPS to the Kubernetes API. The helper-to-broker hop uses HTTP inside the
  cluster. Use a service mesh or equivalent transport protection if your threat
  model requires encryption for that hop.
- Model instructions request approval, but there is no independently enforced
  human approval service. An agent compromise or prompt injection can use its
  token to restart allowed targets. Keep the allowlist short and exclude
  security-sensitive workloads.
- Provider credentials, conversations, workload metadata, and outgoing prompts
  are private instance data. Understand provider retention and costs. Never
  publish the agent PVC, transcripts, rendered configuration, or local logs.

## Optional alert router

`hermes-alert-router` is a separate optional module using the companion
`ghcr.io/vitofico/hermes-agent-router` image, pinned by digest through its image
override and catalog secret contract. Remote chat and restart requests do not need
it. Agent and router share generated `HERMES_WEBHOOK_SECRET`; the agent also
receives it as upstream `WEBHOOK_SECRET`. The configured `uptime-kuma` webhook
route authenticates with this secret, explicitly disables tools, and logs its response
when the selected image satisfies the no-tools contract above. Alerts
provide evidence; they do not authorize repairs. See the
[official webhook contract](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks)
before adapting a router. A signed payload is not a trusted instruction.

## Acceptance checks

Unit tests cover authentication, strict targets and bodies, fixed patches,
cooldown, API transport and error redaction. Schema validation includes enabled
broker and Telegram configuration. The kind repair smoke test exercises the
actual broker against a disposable Kubernetes API independently of Hermes.
It does not test a real model or Telegram account.

Before relying on remote recovery, test your chosen image loading the skill,
successful status reads, blocked direct writes, a signed webhook with no tool execution, rejected unauthorized chat users,
one approved restart, and observed application readiness. No real provider or
chat messages are sent by default integration tests.
