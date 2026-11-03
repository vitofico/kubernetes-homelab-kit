# Contributing

Contributions that make self-hosting easier to understand, configure, and verify
are welcome. Describe the user problem, resulting behavior, and how you tested
it. Keep application-specific requirements visible in `catalog.yaml`.

## Local development

Run `make tools` and use `local/site.yaml` for synthetic test settings. Keep
changes focused. New inputs belong in the central schema, with validation and
example documentation. New integrations must declare credentials, image
overrides, dependencies, and kind limitations explicitly.

Before proposing changes, run:

```sh
make lint test-unit privacy
make validate-all VALIDATE_FLAGS=--prepare
make test-kind
```

Use `make test-kind-all` for changes affecting the broader catalog. The first
schema pass downloads pinned tools and schemas; later validation works offline.
Explain tests you could not run and required external acceptance checks. Do not
represent schema validation as runtime certification.

## Privacy and dependencies

Use reserved example domains and addresses. Never attach real configuration,
encrypted instance secrets, kubeconfigs, private screenshots, conversation
transcripts, or raw cluster logs to a public issue or change. Review the final
diff and scan the publication snapshot and Git history. Report vulnerabilities
through the private process in [SECURITY.md](SECURITY.md).

Preserve third-party licenses and notices when updating vendored definitions.
Record sources in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Permission and
remote-automation changes need an explicit description of authority and
meaningful negative tests.
