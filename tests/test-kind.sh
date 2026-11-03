#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON=${PYTHON:-$ROOT/.venv/bin/python}
else
  PYTHON=${PYTHON:-python3}
fi
export PYTHON
mkdir -p "$ROOT/local/test-work"
WORK=$(mktemp -d "$ROOT/local/test-work/run.XXXXXX")
SUFFIX=$(printf '%s' "${WORK##*.}" | tr '[:upper:]' '[:lower:]')
NAME=${TEST_CLUSTER_NAME:-kubernetes-homelab-kit-review-$SUFFIX}
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "Invalid test cluster name" >&2; exit 1; }
export TEST_CLUSTER_NAME="$NAME"
LOCK_ROOT=${TMPDIR:-/tmp}/kubernetes-homelab-kit-kind-locks
KIND_TEST_LOCK=$LOCK_ROOT/$NAME.lock
export KIND_TEST_LOCK
LOCK_HELD=0
cleanup() {
  local status=$?
  if [[ -f "$WORK/created-cluster" ]]; then
    kubectl --kubeconfig "$WORK/kubeconfig" get pods -A > "$WORK/pods.txt" 2>&1 || true
    kubectl --kubeconfig "$WORK/kubeconfig" get events -A > "$WORK/events.txt" 2>&1 || true
    if [[ "$status" != 0 && "${KEEP_KIND_ON_FAILURE:-0}" == 1 ]]; then
      printf 'Retained owned test cluster %s after failure for diagnostics.\n' "$NAME"
      printf 'Clean it explicitly with: kind delete cluster --name %q --kubeconfig %q\n' "$NAME" "$WORK/kubeconfig"
    else
      kind delete cluster --name "$NAME" --kubeconfig "$WORK/kubeconfig" > "$WORK/cleanup.log" 2>&1 || status=1
    fi
  fi
  if [[ "$LOCK_HELD" == 1 ]]; then
    rm -f "$KIND_TEST_LOCK/owner"
    rmdir "$KIND_TEST_LOCK" || status=1
  fi
  printf 'Test exit status: %s\nPrivate diagnostics: %s\n' "$status" "$WORK"
  exit "$status"
}
trap cleanup EXIT
mkdir -p "$LOCK_ROOT"
if ! mkdir "$KIND_TEST_LOCK" 2>/dev/null; then
  echo "Another test run owns cluster name $NAME; choose a different TEST_CLUSTER_NAME." >&2
  exit 1
fi
LOCK_HELD=1
printf '%s\n' "$WORK" > "$KIND_TEST_LOCK/owner"
for cmd in docker kind kubectl curl sops age-keygen; do command -v "$cmd" >/dev/null; done
mkdir -p local/test-work
./tests/kind/bootstrap.sh "$WORK" > "$WORK/bootstrap.log" 2>&1
./scripts/lab configure --non-interactive --profile kind --config "$WORK/site.yaml"
"$PYTHON" - "$WORK/site.yaml" "$NAME" <<'PY'
import pathlib,sys,yaml
p=pathlib.Path(sys.argv[1]); c=yaml.safe_load(p.read_text()); c['cluster_name']=sys.argv[2]; p.write_text(yaml.safe_dump(c,sort_keys=False))
PY
./scripts/lab up --config "$WORK/site.yaml" --kubeconfig "$WORK/kubeconfig" > "$WORK/up.log" 2>&1
"$PYTHON" tests/kind/assertions.py --kubeconfig "$WORK/kubeconfig" --work "$WORK"
./scripts/lab render --config "$WORK/site.yaml" --output "$WORK/render-before"
./scripts/lab up --config "$WORK/site.yaml" --kubeconfig "$WORK/kubeconfig" > "$WORK/up-repeat.log" 2>&1
./scripts/lab render --config "$WORK/site.yaml" --output "$WORK/render-repeat"
cmp "$WORK/render-before/resources.yaml" "$WORK/render-repeat/resources.yaml"
echo 'PASS repeated deployment and deterministic rendering'
"$PYTHON" tests/kind/hermes.py "$WORK/kubeconfig" "$WORK/site.yaml" "$WORK"
"$PYTHON" - "$WORK/site.yaml" <<'PY'
import pathlib,sys,yaml
p=pathlib.Path(sys.argv[1]); c=yaml.safe_load(p.read_text()); c['domain']='changed.example.test'; p.write_text(yaml.safe_dump(c,sort_keys=False))
PY
./scripts/lab up --config "$WORK/site.yaml" --kubeconfig "$WORK/kubeconfig" > "$WORK/up-changed.log" 2>&1
"$PYTHON" - "$WORK/kubeconfig" <<'PY'
import json,subprocess,sys
items=json.loads(subprocess.check_output(['kubectl','--kubeconfig',sys.argv[1],'get','ingress','-A','-o','json']))['items']
assert items, 'Expected rendered application ingresses'
assert all(rule['host'].endswith('.changed.example.test') for item in items for rule in item['spec']['rules'])
print('PASS configuration change applied to application ingresses')
PY
if [[ -n "${KIND_TEST_EXTRA_SCRIPT:-}" ]]; then
  bash "$KIND_TEST_EXTRA_SCRIPT" "$WORK/kubeconfig" "$WORK/site.yaml" "$WORK"
fi
printf 'All Docker kind integration checks passed.\n'
