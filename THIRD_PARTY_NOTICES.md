# Third-party material

The MIT license applies to this project's original tooling and documentation. Bundled upstream Kubernetes definitions retain their applicable upstream licenses. Container images are separate dependencies and are not distributed in this repository; their names do not relicense their contents.

Upstream manifest sources include:

| Project | Bundled version | Source | License |
|---|---|---|---|
| Argo Workflows | v4.0.15 | https://github.com/argoproj/argo-workflows | Apache-2.0 |
| cert-manager | v1.21.3 | https://github.com/cert-manager/cert-manager | Apache-2.0 |
| MetalLB | v0.16.1 | https://github.com/metallb/metallb | Apache-2.0 |
| Traefik | v3.7.16 | https://github.com/traefik/traefik | MIT |
| Nextcloud Helm chart | 8.9.0 | https://github.com/nextcloud/helm | AGPL-3.0 |
| Bitnami charts | Redis chart 21.1.3 / MariaDB chart 20.5.5 | https://github.com/bitnami/charts | Apache-2.0 |

Apache-2.0 terms are included in `LICENSES/Apache-2.0.txt`. Preserve upstream copyright, license, and NOTICE material when updating vendored definitions. Review the extracted application catalog and upstream provenance before public release, particularly any added or replaced charts and custom images.

The Nextcloud application definitions include material derived from the Nextcloud Helm chart (8.9.0), licensed under AGPL-3.0, and Bitnami MariaDB (20.5.5) and Redis (21.1.3) chart templates under Apache-2.0. These definitions were modified on 2026-11-03: flattened into a standalone Kustomize base, generalized for operator-supplied configuration, and stripped of instance-specific state. The Nextcloud-derived material retains AGPL-3.0 terms; it is not relicensed under MIT. The complete AGPL-3.0 text is in `LICENSES/AGPL-3.0.txt`.
