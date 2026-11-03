#!/usr/bin/env python3
"""Opt-in sequential runtime smoke tests for kind-compatible catalog modules.

Invoked only by the disposable kind harness with its private kubeconfig.
"""
import base64
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)

def check_ingresses(kube, docs, directory):
    ingresses = [d for d in docs if d.get('kind') == 'Ingress']
    if not ingresses:
        return 0
    prefix = ['kubectl', '--kubeconfig', str(kube)]
    for namespace in sorted({d['metadata']['namespace'] for d in ingresses}):
        subprocess.run(prefix + ['-n', namespace, 'wait', '--for=condition=Ready', 'certificate', '--all', '--timeout=120s'], capture_output=True, check=True)
    encoded = subprocess.check_output(prefix + ['-n', 'cert-manager', 'get', 'secret', 'homelab-root-ca', '-o', 'go-template={{index .data "tls.crt"}}'], text=True)
    ca = directory / 'test-ca.crt'
    ca.write_bytes(base64.b64decode(encoded))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with (directory / 'ingress-forward.log').open('w') as log:
        forward = subprocess.Popen(prefix + ['-n', 'traefik', 'port-forward', '--address=127.0.0.1', 'svc/traefik', str(port)+':443'], stdout=log, stderr=log)
        try:
            count = 0
            for ingress in ingresses:
                for rule in ingress['spec'].get('rules', []):
                    host = rule['host']
                    for route in rule.get('http', {}).get('paths', []):
                        path = route.get('path') or '/'
                        if not path.startswith('/') or any(char in path for char in ('*', '(', '[', '\\')):
                            raise lab.Error('Regex ingress paths require an explicit HTTP test fixture')
                        deadline = time.monotonic() + 90
                        while True:
                            response = subprocess.run(['curl', '--silent', '--show-error', '--noproxy', '*', '--cacert', str(ca), '--connect-to', host+':443:127.0.0.1:'+str(port), '--max-time', '8', '--dump-header', str(directory/'https-headers.txt'), '--output', str(directory/'https-body.html'), '--write-out', '%{http_code}', 'https://'+host+path], capture_output=True, text=True)
                            if response.returncode == 0 and response.stdout in {'200','201','204','301','302','303','307','308','401','403'}:
                                count += 1
                                break
                            if time.monotonic() >= deadline:
                                raise lab.Error(f'Ingress {host}{path} failed trusted TLS/HTTP check (code {response.stdout or "none"})')
                            time.sleep(2)
            return count
        finally:
            forward.terminate()
            forward.wait(timeout=10)

def capture_failure(kube, namespaces, directory):
    """Retain bounded diagnostics before removing this disposable case's resources."""
    target = directory / 'diagnostics'
    target.mkdir(exist_ok=True, mode=0o700)
    prefix = ['kubectl', '--kubeconfig', str(kube)]
    def save(arguments, filename):
        try:
            response = subprocess.run(prefix + arguments, capture_output=True, text=True, timeout=20)
            (target / filename).write_text(response.stdout + response.stderr)
            return response.stdout if response.returncode == 0 else ''
        except subprocess.TimeoutExpired:
            (target / filename).write_text('Diagnostic command exceeded its timeout.\n')
            return ''
    for namespace in sorted(namespaces):
        raw = save(['-n', namespace, 'get', 'pods', '-o', 'json'], namespace+'-pods.json')
        save(['-n', namespace, 'get', 'events', '--sort-by=.metadata.creationTimestamp'], namespace+'-events.txt')
        try:
            pods = json.loads(raw).get('items', []) if raw else []
        except json.JSONDecodeError:
            pods = []
        for pod in pods:
            name = pod['metadata']['name']
            for status in pod.get('status', {}).get('containerStatuses', []) + pod.get('status', {}).get('initContainerStatuses', []):
                container = status['name']
                if status.get('state', {}).get('running') or status.get('state', {}).get('terminated'):
                    save(['-n', namespace, 'logs', name, '-c', container, '--tail=150'], name+'-'+container+'.log')
                if status.get('restartCount', 0):
                    save(['-n', namespace, 'logs', name, '-c', container, '--previous', '--tail=150'], name+'-'+container+'-previous.log')

def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: test-catalog.py KUBECONFIG BASE_CONFIG WORK')
    kube, base, work = [Path(x).resolve() for x in sys.argv[1:]]
    work = lab.local_path(work)
    if not (work / 'created-cluster').is_file() or not kube.is_relative_to(work) or not base.is_relative_to(work):
        raise SystemExit('Only an owned disposable harness work directory and its kubeconfig/config are accepted')
    cfg = lab.config(base)
    if cfg['profile'] != 'kind':
        raise SystemExit('Only disposable kind profiles are accepted')
    context = subprocess.check_output(['kubectl','--kubeconfig',str(kube),'config','current-context'],text=True).strip()
    if context != 'kind-' + cfg['cluster_name']:
        raise SystemExit('Kubeconfig does not match the disposable cluster')
    base_apps = set(cfg['applications'])
    catalog = lab.catalog()
    baseline_ns = {catalog[a]['namespace'] for a in base_apps}
    baseline_pvs = {doc['metadata']['name'] for doc in lab.documents(cfg) if doc.get('kind') == 'PersistentVolume'}
    selected = {name.strip() for name in os.environ.get('CATALOG_TEST_APPS', '').split(',') if name.strip()}
    eligible = {name for name, entry in catalog.items() if entry.get('kind_supported') and name not in base_apps}
    if selected - eligible:
        raise SystemExit('CATALOG_TEST_APPS must select supported modules outside the baseline')
    report=[]
    for app, entry in catalog.items():
        if selected and app not in selected:
            continue
        if not entry.get('kind_supported') or app in base_apps:
            continue
        directory=work / ('catalog-' + app)
        directory.mkdir(parents=True,exist_ok=True)
        site=work / 'catalog-site.yaml'
        candidate=dict(cfg, applications=[app])
        site.write_text(yaml.safe_dump(candidate,sort_keys=False))
        expanded=lab.config(site)
        env=dict(os.environ)
        cmd=[str(ROOT/'scripts/lab')]
        log=directory/'runtime.log'
        result={'application':app,'status':'failed'}
        try:
            with log.open('w') as output:
                subprocess.run(cmd+['configure','--non-interactive','--config',str(site)],stdout=output,stderr=output,env=env,check=True)
                values=lab.secret_store(expanded,site)
                # This test has no external providers: populate required identity fields
                # with synthetic local values. Never use production credentials.
                for appdata in values.values():
                    for secret in appdata.values():
                        for key,value in list(secret.items()):
                            if not value:
                                secret[key]='test@example.test' if 'email' in key.lower() else 'synthetic-user'
                if values:
                    keyfile=site.parent/'age.agekey'
                    recipient=lab.run(['age-keygen','-y',keyfile]).strip()
                    encrypted=lab.run(['sops','encrypt','--age',recipient,'--input-type','yaml','--output-type','yaml','/dev/stdin'],data=lab.dump(values))
                    lab.write_private(site.parent/'secrets.sops.yaml',encrypted)
                # Authentik's declared startup-probe budget reaches 1320s;
                # leave room for dependency readiness within this bounded case.
                subprocess.run(cmd+['up','--config',str(site),'--kubeconfig',str(kube)],stdout=output,stderr=output,env=env,check=True,timeout=1800)
            result['https_endpoints_checked']=check_ingresses(kube,lab.documents(expanded),directory)
            result['status']='passed'
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired,lab.Error) as error:
            result['error']=type(error).__name__
            capture_failure(kube, {catalog[a]['namespace'] for a in expanded['applications']} - baseline_ns, directory)
            with log.open('a') as output:
                output.write('\nRuntime check failed: '+str(error)+'\n')
        finally:
            # Module namespaces and static PVs are scoped to this generated case.
            # Keep shared baseline namespaces/CRDs/controllers intact.
            namespaces={catalog[a]['namespace'] for a in expanded['applications']}-baseline_ns
            for namespace in sorted(namespaces):
                deleted = subprocess.run(['kubectl','--kubeconfig',str(kube),'delete','namespace',namespace,'--ignore-not-found','--wait=true','--timeout=120s'],capture_output=True,text=True)
                if deleted.returncode:
                    result.update(status='failed', cleanup_error='namespace deletion failed: ' + namespace)
            for doc in lab.documents(expanded):
                if doc.get('kind')=='PersistentVolume' and doc['metadata']['name'] not in baseline_pvs:
                    deleted = subprocess.run(['kubectl','--kubeconfig',str(kube),'delete','pv',doc['metadata']['name'],'--ignore-not-found','--wait=false'],capture_output=True,text=True)
                    if deleted.returncode:
                        result.update(status='failed', cleanup_error='PV deletion failed: ' + doc['metadata']['name'])
        report.append(result)
        (work/'catalog-runtime.json').write_text(json.dumps(report,indent=2)+'\n')
        print('CATALOG '+app+': '+result['status'],flush=True)
    return any(x['status']!='passed' for x in report)

if __name__=='__main__':
    sys.exit(main())
