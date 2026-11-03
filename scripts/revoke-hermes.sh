#!/usr/bin/env bash
# Explicitly revoke only this kit's optional restart broker. No general pruning.
set -euo pipefail
KUBE=${1:?usage: revoke-hermes.sh EXPLICIT_KUBECONFIG}
[[ -f "$KUBE" ]] || { echo 'Provide an existing explicit kubeconfig.' >&2; exit 1; }
SCOPE='app.kubernetes.io/part-of=kubernetes-homelab-kit,app.kubernetes.io/component=hermes-repair'
# Revoke grants first. Capture inventory before iteration so list failures stop
# the command; process substitution would hide such failures from set -e.
for kind in rolebindings roles; do
  INVENTORY=$(kubectl --kubeconfig "$KUBE" get "$kind" --all-namespaces -l "$SCOPE" -o 'jsonpath={range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}')
  while read -r namespace name; do
    [[ -n "$namespace" && -n "$name" ]] || continue
    kubectl --kubeconfig "$KUBE" -n "$namespace" delete "$kind" "$name" --ignore-not-found=true
  done <<< "$INVENTORY"
done
kubectl --kubeconfig "$KUBE" -n agent delete deployment,service,serviceaccount,networkpolicy,secret -l "$SCOPE" --ignore-not-found=true --wait=true
echo 'Restart broker and its grants revoked. Local encrypted credentials are preserved.'
echo 'Remove repair_targets before the next deployment; rotate REPAIR_TOKEN before re-enabling.'
