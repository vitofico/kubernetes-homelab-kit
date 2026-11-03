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
NAME=${TEST_CLUSTER_NAME:-homelab-kit-review}
mkdir -p "$ROOT/local/test-work"
WORK=$(mktemp -d "$ROOT/local/test-work/run.XXXXXX")
export TEST_CLUSTER_NAME="$NAME"
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
  printf 'Test exit status: %s\nPrivate diagnostics: %s\n' "$status" "$WORK"
  exit "$status"
}
trap cleanup EXIT
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
