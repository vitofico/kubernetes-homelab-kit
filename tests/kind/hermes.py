#!/usr/bin/env python3
"""Exercise rendered Hermes init, identities and restart broker without a model."""
import copy
import importlib.util
import ipaddress
import json
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
kubeconfig, site, work = sys.argv[1:]
kubeconfig, site, work = [Path(value).resolve() for value in (kubeconfig, site, work)]
work = lab.local_path(work)
if (not (work / 'created-cluster').is_file()
        or not kubeconfig.is_relative_to(work) or not site.is_relative_to(work)):
    raise SystemExit('Only an owned disposable harness work directory and its kubeconfig/config are accepted')


def k(*args, data=None, check=True):
    result = subprocess.run(['kubectl', '--kubeconfig', kubeconfig, *args],
                            input=data, text=True, capture_output=True)
    if check and result.returncode:
        # These fixtures contain authentication values; never echo failed input.
        if not data or '\nkind: Secret\n' not in data:
            lab.write_private(work / 'hermes-kubectl-error.log', result.stderr)
        raise RuntimeError('kubectl operation failed: ' + ' '.join(args[:3]))
    return result


def obj(*args):
    return json.loads(k(*args, '-o', 'json').stdout)


cfg = lab.load(site)
if cfg['profile'] != 'kind' or (work / 'created-cluster').read_text().strip() != cfg['cluster_name']:
    raise SystemExit('Only the recorded disposable kind profile is accepted')
if k('config', 'current-context').stdout.strip() != 'kind-' + cfg['cluster_name']:
    raise SystemExit('Disposable kind context must match the recorded test cluster')
cfg.update(profile='server', domain='repair.example.net',
           applications=['audiobookshelf', 'hermes-agent'])
cfg['storage']['class_name'] = 'standard'
cfg['images']['IMAGE_HERMES_AGENT'] = 'python:3.13-alpine'
addresses = [obj('-n', 'default', 'get', 'service/kubernetes')['spec']['clusterIP']]
for item in obj('-n', 'default', 'get', 'endpointslices',
                '-l', 'kubernetes.io/service-name=kubernetes')['items']:
    addresses.extend(address for endpoint in item['endpoints'] for address in endpoint['addresses'])
cfg['hermes'] = {'api_cidrs': [str(ipaddress.ip_network(address)) for address in sorted(set(addresses))],
                 'repair_targets': [{'namespace': 'audiobookshelf', 'deployment': 'audiobookshelf'}],
                 'telegram': {'enabled': False, 'allowed_users': []}}
path = Path(work) / 'hermes-fixture.yaml'
lab.write_private(path, lab.dump(cfg))
docs = lab.documents(lab.config(path))
docs = [d for d in docs if d.get('metadata', {}).get('namespace') == 'agent'
        or d.get('metadata', {}).get('name') == 'agent' and d['kind'] == 'Namespace'
        or d['kind'] in ('Role', 'RoleBinding') and d['metadata']['name'].startswith('hermes-repair')
        or d['kind'] in ('ClusterRole', 'ClusterRoleBinding') and d['metadata']['name'] == 'hermes-agent']
agent = next(d for d in docs if d['kind'] == 'Deployment' and d['metadata']['name'] == 'hermes-agent')
# Preserve actual init/volumes/UID/network/identity. Replace only the external
# gateway with an inert Python process so provider credentials are never used.
container = agent['spec']['template']['spec']['containers'][0]
container['command'] = ['python3', '-c', 'import time; time.sleep(1200)']
container.pop('args', None)
for name in ('livenessProbe', 'readinessProbe', 'startupProbe'):
    container.pop(name, None)
token = secrets.token_urlsafe(32)
for name, values in {
    'ai-provider-keys': {'anthropic': 'synthetic-not-used'},
    'hermes-agent-secrets': {'HERMES_WEBHOOK_SECRET': secrets.token_urlsafe(32)},
    'hermes-repair-credentials': {'REPAIR_TOKEN': token},
}.items():
    docs.append({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'namespace': 'agent', 'name': name},
                 'type': 'Opaque', 'stringData': values})
    if name == 'hermes-repair-credentials':
        docs[-1]['metadata']['labels'] = {'app.kubernetes.io/part-of': 'kubernetes-homelab-kit',
                                         'app.kubernetes.io/component': 'hermes-repair'}
manifest = lab.dump_all(docs, sort_keys=False)
lab.write_private(Path(work) / 'hermes-resources.yaml', manifest)
k('apply', '--server-side', '-f', '-', data=lab.dump_all([d for d in docs if d['kind'] != 'Secret'], sort_keys=False))
k('apply', '--server-side', '-f', '-', data=lab.dump_all([d for d in docs if d['kind'] == 'Secret'], sort_keys=False))
for name in ('hermes-agent', 'hermes-repair'):
    k('-n', 'agent', 'rollout', 'status', 'deployment/' + name, '--timeout=300s')


def agent_python(code):
    return k('-n', 'agent', 'exec', 'deployment/hermes-agent', '--', 'python3', '-c', code).stdout


agent_python("from pathlib import Path; import os; "
             "assert os.getuid()==10000; "
             "assert Path(os.environ['HERMES_HOME']+'/skills/cluster-ops/SKILL.md').is_file(); "
             "p=Path(os.environ['HERMES_HOME'])/'synthetic-write-probe'; p.write_text('ok'); p.unlink()")
print('PASS Hermes nonroot volume initialization and mounted skill', flush=True)

for identity, verb, resource, namespace, expected in [
    ('hermes-agent', 'patch', 'deployment/audiobookshelf', 'audiobookshelf', 'no'),
    ('hermes-agent', 'get', 'secrets', 'agent', 'no'),
    ('hermes-repair', 'patch', 'deployment/audiobookshelf', 'audiobookshelf', 'yes'),
    ('hermes-repair', 'patch', 'deployment/uptime-kuma', 'uptime-kuma', 'no'),
    ('hermes-repair', 'patch', 'deployment/hermes-agent', 'agent', 'no'),
]:
    result = k('auth', 'can-i', verb, resource, '-n', namespace,
               '--as=system:serviceaccount:agent:' + identity, check=False)
    assert result.stdout.strip() == expected, (identity, verb, namespace)
print('PASS Hermes read-only and named broker RBAC boundaries', flush=True)

status = k('-n', 'agent', 'exec', 'deployment/hermes-agent', '--', 'python3',
           '/opt/hermes-ops/ops.py', 'status', 'audiobookshelf', 'audiobookshelf')
assert 'audiobookshelf' in status.stdout
before = copy.deepcopy(obj('-n', 'audiobookshelf', 'get', 'deployment/audiobookshelf')['spec'])
probe = '''import json, os, urllib.request, urllib.error
url=os.environ['REPAIR_URL']+'/restart'
def request(payload, auth):
    req=urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST',
        headers={'Authorization':'Bearer '+auth,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=15) as response: return response.status
    except urllib.error.HTTPError as error: return error.code
target={'namespace':'audiobookshelf','deployment':'audiobookshelf'}
assert request(target,'invalid')==401
assert request({'namespace':'uptime-kuma','deployment':'uptime-kuma'},os.environ['REPAIR_TOKEN'])==403
assert request(dict(target, image='hostile'),os.environ['REPAIR_TOKEN'])==400
assert request(target,os.environ['REPAIR_TOKEN'])==202
assert request(target,os.environ['REPAIR_TOKEN'])==429
'''
agent_python(probe)
after = copy.deepcopy(obj('-n', 'audiobookshelf', 'get', 'deployment/audiobookshelf')['spec'])
annotation = after['template']['metadata']['annotations'].pop('kubernetes-homelab-kit/restartedAt')
assert annotation
if not after['template']['metadata']['annotations']:
    after['template']['metadata'].pop('annotations')
assert before == after, 'Broker modified fields beyond its fixed restart annotation'
k('-n', 'audiobookshelf', 'rollout', 'status', 'deployment/audiobookshelf', '--timeout=180s')
print('PASS real API status, broker auth/target/body rejection, fixed restart and cooldown', flush=True)

# A client lacking the agent label cannot reach the broker, even in its namespace.
denied = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'repair-denied', 'namespace': 'agent'},
          'spec': {'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                   'containers': [{'name': 'client', 'image': 'python:3.13-alpine',
                                    'command': ['python3', '-c', 'import time; time.sleep(300)']}]}}
k('apply', '-f', '-', data=lab.dump(denied))
k('-n', 'agent', 'wait', 'pod/repair-denied', '--for=condition=Ready', '--timeout=180s')
blocked = k('-n', 'agent', 'exec', 'repair-denied', '--', 'python3', '-c',
            "import urllib.request; urllib.request.urlopen('http://hermes-repair.agent.svc:8080/health',timeout=3)", check=False)
assert blocked.returncode != 0, 'Broker NetworkPolicy allowed an unrelated client'
k('-n', 'agent', 'delete', 'pod/repair-denied', '--wait=false')
print('PASS broker NetworkPolicy rejects unrelated client', flush=True)
subprocess.run([str(ROOT / 'scripts/revoke-hermes.sh'), str(kubeconfig)],
               capture_output=True, text=True, check=True)
assert k('-n', 'agent', 'get', 'deployment/hermes-repair', check=False).returncode != 0
assert k('auth', 'can-i', 'patch', 'deployment/audiobookshelf', '-n', 'audiobookshelf',
         '--as=system:serviceaccount:agent:hermes-repair', check=False).stdout.strip() == 'no'
assert k('-n', 'agent', 'get', 'secret/hermes-repair-credentials', check=False).returncode != 0
assert obj('-n', 'agent', 'get', 'deployment/hermes-agent')['metadata']['name'] == 'hermes-agent'
assert obj('-n', 'audiobookshelf', 'get', 'deployment/audiobookshelf')['status']['readyReplicas'] == 1
print('PASS explicit broker revocation removes grants and token, preserves agent and app', flush=True)
