# Local review candidate

This repository is intended for local review before any publication. It has fresh Git history, no configured remote, and a synthetic example configuration. Instance-specific operational records and encrypted production secrets are excluded.

## Review before publication

- Read `catalog.yaml` for optional applications, image overrides, required credentials, dependencies, and environment restrictions.
- Review all eligible files, including scripts, configuration, workflow definitions, application ConfigMaps, container arguments, and upstream notices.
- Run `make privacy`, `make lint`, `make test-unit`, `make validate`, `make validate-all`, and `make test-kind-all`. Prepare the pinned schema cache once as described in `tools/validate-catalog.md`.
- Run an offline secret scanner on both an export of eligible files and the complete new Git history. Review findings individually; do not suppress documentation or whole application directories.
- Inspect the final archive and commit identity. Keep private audit evidence and any instance-specific identifier lists outside the repository.
- Confirm the MIT license for original material and third-party attribution before publication.

## Scope

The application definitions are simplified into flat Kustomize modules. They are templates rather than a dump of a running cluster. Personal schedules, initialized account settings, identity-provider blueprints, monitoring enrollment, legacy storage paths, and production credentials are excluded. Optional integrations require the operator's own settings and acceptance tests.

The server profile assumes an existing Kubernetes cluster and storage services. Docker kind tests validate the shared deployment flow with disposable local storage. They do not establish compatibility with physical disks, a particular router, camera hardware, real DNS APIs, external VPNs, or production backup destinations. See `testing.md` for actual executed coverage.

## Publication boundary

Local runtime configuration, credentials, keys, manifests, cluster logs, and test diagnostics belong under ignored directories. Public CI must use synthetic data. Do not merge private operational Git history into this repository or publish a private identifier denylist as a test fixture.

Creation of a public remote and the first push are held for the owner's review of the sanitized version.

Argo uses client authentication; operators must supply their own Kubernetes authentication token when accessing its UI. Application enrollment and account creation remain application-specific.
