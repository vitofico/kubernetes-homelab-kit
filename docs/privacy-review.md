# Privacy review

This is a fresh repository assembled from an explicit set of Kubernetes application definitions and new tooling. It has no imported commits, operational records, ignored credential files, or production secret ciphertext.

## Export decisions

- Retained generic workload definitions, service wiring, RBAC, ingress, volume templates, and upstream controller definitions.
- Flattened application bases into Kustomize modules and replaced instance endpoints with named configuration inputs.
- Removed initialized media application configuration, embedded account settings, identity-provider enrollment blueprints, personal scheduled jobs, monitoring enrollment scripts, legacy personal storage mounts, and private agent context.
- Rebuilt generic DNS and agent configuration instead of copying their initialized state.
- Removed production Kubernetes Secrets, SOPS recipients, and ciphertext. Credentials are generated or supplied in the ignored local workspace.
- Removed personal image registry references. Modules requiring a compatible custom image demand an explicit operator override and stay out of the default kind profile.
- Preserved upstream license and attribution requirements separately from original project tooling.

The catalog describes module requirements and exclusions. Changes to storage layout intentionally require operator review rather than retaining paths that identify an existing deployment.

## Verification layers

The public audit checks eligible files for private key material, opaque production ciphertext, local user paths, hardware identifiers, non-example email addresses, symlinks, binaries, and accidentally tracked local configuration. Regression checks also reject regional defaults and hardcoded mail-provider domains in application bases. An offline Gitleaks scan complements these checks. Neither check alone establishes that a file is appropriate to publish.

A separate private review compares the export against instance identifiers discovered in the original source. That identifier list and detailed extraction ledger are deliberately kept outside this repository. The original source's tracked file hashes and Git revision are checked separately to verify preservation.

The review boundary includes all tracked files and commit metadata, plus the final archive. Local credentials, rendered configurations, diagnostics, test kubeconfigs, and virtual environments must remain excluded. Re-run the checks whenever candidate contents change.

Publication is held for owner review. This record is a description of the review process, not a substitute for reviewing the actual contents and test coverage.
