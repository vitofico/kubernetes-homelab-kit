# Complete catalog schema validation

After `make tools`, prepare the validator and schema cache once:

```sh
make validate-all VALIDATE_FLAGS=--prepare
```

Subsequent validation is offline:

```sh
make validate-all
```

The helper renders every catalog application together using synthetic server
configuration. Required custom image references are replaced with a schema-only
fixture; this does not test or claim runtime compatibility of that image. No
cluster is accessed, credentials are not generated, and nothing is deployed.
Generated configuration, manifests, schemas and `report.json` stay under ignored
`local/schema-validation/`.

Kubeconform is pinned to **v0.8.0**. An installed matching version is reused;
otherwise `--prepare` downloads the official release for Linux/macOS amd64/arm64
and verifies its release SHA-256 checksum before extracting the binary into
ignored `local/tools/`.

Native schemas are pinned to Kubernetes **1.37.0** and commit
`e0c72a1bc5f409d1685c0e1323460c8a98cd7459` of the upstream schema registry.
The upstream standalone generator omits the recursive CustomResourceDefinition
root schema. The helper bundles its official non-standalone root with the same
pinned version's recursive definitions instead. All schema references must be
internal, and Kubeconform receives only local schema paths during validation.

Custom resources use the OpenAPI schemas from the CRDs shipped in this catalog,
with OpenAPI nullable and integer-or-string extensions converted to JSON Schema.
There is no missing-schema ignore flag or resource skip list. Every run also
checks that invalid native and MetalLB resources fail validation, and that an
unregistered custom kind fails for a missing schema.

Schema validation complements the Docker kind runtime tests. Admission webhooks,
CEL rules, controller behavior, hardware integrations, and external-provider
credentials still require their applicable runtime tests.

Primary references: [Kubeconform usage and schema limitations](https://github.com/yannh/kubeconform),
[pinned native schema source](https://github.com/yannh/kubernetes-json-schema/tree/e0c72a1bc5f409d1685c0e1323460c8a98cd7459/v1.37.0-standalone-strict).
