#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${1:?usage: bootstrap.sh WORK_DIRECTORY}
NAME=${TEST_CLUSTER_NAME:-homelab-kit-review}
mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
export KIND_EXPERIMENTAL_PROVIDER=docker
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "Invalid test cluster name" >&2; exit 1; }
CLUSTERS=$(kind get clusters)
if printf '%s\n' "$CLUSTERS" | grep -Fxq "$NAME"; then
  echo "Refusing to replace existing cluster $NAME" >&2
  exit 1
fi
# Official Docker Hub and Quay mirrors have identical index and per-platform
# digests. Let node containerd fetch the exact artifacts directly; importing host
# multi-platform caches can fail when the host stores only one platform's layers.
CNI_NAMES=(calico third-party-cni-plugins node)
CNI_REFS=(
  docker.io/calico/calico@sha256:4ebc20730127bc9e8bf1db5f5a2d8b58cf37fe0e891b3c7b5471992221d213e5
  docker.io/calico/third-party-cni-plugins@sha256:0f32f48279484c0d6f0e0d0e767b2d0e1a6c6ca34cb3421a61b989d992f3df41
  docker.io/calico/node@sha256:565d8db3200e443955557f8f28ff44ab8a4a456754bc74a19c4f2f3444af9b1c
)
printf '%s\n' "$NAME" > "$WORK/created-cluster"
kind create cluster --name "$NAME" --image kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5 --config "$ROOT/tests/kind/cluster.yaml" --kubeconfig "$WORK/kubeconfig" --wait 0s
printf '%s\n' "$NAME" > "$WORK/created-cluster"
# CNI is not installed yet, so nodes are expected to remain NotReady initially.
curl --fail --silent --show-error --location --retry 3 \
  https://raw.githubusercontent.com/projectcalico/calico/v3.33.0/manifests/calico.yaml \
  --output "$WORK/calico.yaml"
EXPECTED=2de8f47595fb9c41b3f47d7b767a1f8e72ecf84057af834738ff12689a234da5
ACTUAL=$(shasum -a 256 "$WORK/calico.yaml" | awk '{print $1}')
[[ "$ACTUAL" == "$EXPECTED" ]] || { echo 'Calico manifest checksum mismatch' >&2; exit 1; }
sed -e 's/# - name: CALICO_IPV4POOL_CIDR/- name: CALICO_IPV4POOL_CIDR/' \
  -e 's@#   value: "[0-9.]*/[0-9]*"@  value: "10.244.0.0/16"@' \
  "$WORK/calico.yaml" > "$WORK/calico-configured.yaml"
for index in "${!CNI_NAMES[@]}"; do
  name=${CNI_NAMES[$index]}
  sed "s|image: quay.io/calico/$name:v3.33.0|image: ${CNI_REFS[$index]}|g" \
    "$WORK/calico-configured.yaml" > "$WORK/calico-rewrite.yaml"
  mv "$WORK/calico-rewrite.yaml" "$WORK/calico-configured.yaml"
done
kubectl --kubeconfig "$WORK/kubeconfig" apply --server-side -f "$WORK/calico-configured.yaml"
kubectl --kubeconfig "$WORK/kubeconfig" -n kube-system rollout status daemonset/calico-node --timeout=900s
kubectl --kubeconfig "$WORK/kubeconfig" wait --for=condition=Ready nodes --all --timeout=180s
printf '%s\n' "$NAME" > "$WORK/created-cluster"
