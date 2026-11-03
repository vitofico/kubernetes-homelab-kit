#!/usr/bin/env python3
"""Black-box assertions against the disposable cluster, never the current context."""
import argparse
import base64
import http.client
import ssl
import json
import pathlib
import socket
import subprocess
import time
import urllib.request
import urllib.parse

p=argparse.ArgumentParser()
p.add_argument('--kubeconfig', required=True)
p.add_argument('--work', required=True)
args=p.parse_args()
work=pathlib.Path(args.work)
results=[]
def k(*a, check=True):
    return subprocess.run(['kubectl','--kubeconfig',args.kubeconfig,*a],capture_output=True,text=True,check=check)
def passed(name):
    results.append({'test':name,'status':'passed'})
    print('PASS '+name,flush=True)
def pod(app):
    items=json.loads(k('-n',app,'get','pods','-l','app='+app,'-o','json').stdout)['items']
    ready=[x for x in items if not x['metadata'].get('deletionTimestamp') and any(c['type']=='Ready' and c['status']=='True' for c in x['status'].get('conditions',[]))]
    assert len(ready)==1, f'{app}: expected exactly one ready nonterminating pod'
    return ready[0]['metadata']['name']
def http_service(namespace,service,port,path='/'):
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); local_port=s.getsockname()[1]
    log=(work/(namespace+'-portforward.log')).open('a')
    process=subprocess.Popen(['kubectl','--kubeconfig',args.kubeconfig,'-n',namespace,'port-forward','--address','127.0.0.1','svc/'+service,f'{local_port}:{port}'],stdout=log,stderr=log)
    try:
        last=None
        for _ in range(30):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{local_port}{path}',timeout=5) as r:
                    assert r.status==200
                    return r.read()
            except Exception as exc:
                last=exc; time.sleep(1)
        raise AssertionError(f'{namespace} HTTP failed: {last}')
    finally:
        process.terminate(); process.wait(timeout=10); log.close()
def https_ingress(app,path):
    ingress=json.loads(k('-n',app,'get','ingress',app,'-o','json').stdout)
    host=ingress['spec']['rules'][0]['host']
    secret=ingress['spec']['tls'][0]['secretName']
    k('-n',app,'wait','certificate/'+secret,'--for=condition=Ready','--timeout=180s')
    data=json.loads(k('-n','cert-manager','get','secret','homelab-root-ca','-o','json').stdout)
    cert=base64.b64decode(data['data']['tls.crt']).decode()
    context=ssl.create_default_context(cadata=cert)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    with (work/'ingress-portforward.log').open('a') as log:
        process=subprocess.Popen(['kubectl','--kubeconfig',args.kubeconfig,'-n','traefik','port-forward','--address','127.0.0.1','svc/traefik',f'{port}:443'],stdout=log,stderr=log)
        try:
            last=None
            for _ in range(30):
                try:
                    route=path
                    for redirect in range(6):
                        with socket.create_connection(('127.0.0.1',port),timeout=5) as raw:
                            with context.wrap_socket(raw,server_hostname=host) as tls:
                                tls.sendall(f'GET {route} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
                                response=http.client.HTTPResponse(tls);response.begin()
                                status=response.status
                                location=response.getheader('Location')
                                response.read()
                        if status in (301,302,303,307,308):
                            assert location, 'Redirect missing Location'
                            destination=urllib.parse.urlsplit(urllib.parse.urljoin(f'https://{host}{route}',location))
                            assert destination.scheme=='https' and destination.hostname==host, 'Unexpected external or insecure redirect'
                            assert destination.port in (None,443), 'Unexpected redirect port'
                            route=urllib.parse.urlunsplit(('', '', destination.path or '/', destination.query, ''))
                            continue
                        assert status==200, f'Ingress returned {status}'
                        return
                    raise AssertionError('HTTPS redirect limit exceeded')
                except Exception as exc:
                    last=exc;time.sleep(1)
            raise AssertionError(f'{app} HTTPS ingress failed: {last}')
        finally:
            process.terminate();process.wait(timeout=10)
try:
    for app,port,path,mount in [('audiobookshelf',80,'/healthcheck','/config'),('uptime-kuma',3001,'/','/app/data')]:
        k('-n',app,'rollout','status','deployment/'+app,'--timeout=300s')
        http_service(app,app,port,path)
        passed(app+' HTTP readiness')
        https_ingress(app,path)
        passed(app+' HTTPS ingress with trusted certificate and hostname verification')
        before=pod(app)
        marker=mount+'/homelab-kit-persistence-test'
        k('-n',app,'exec',before,'--','sh','-c','printf synthetic-test-data > "$1"','sh',marker)
        k('-n',app,'delete','pod',before,'--wait=true')
        k('-n',app,'rollout','status','deployment/'+app,'--timeout=180s')
        after=pod(app)
        assert before!=after
        assert k('-n',app,'exec',after,'--','cat',marker).stdout=='synthetic-test-data'
        k('-n',app,'exec',after,'--','rm',marker)
        passed(app+' persistence after pod recreation')
    # Two otherwise identical clients prove that the namespace policy permits only
    # the ingress namespace. A CNI enforcing NetworkPolicy is mandatory.
    for ns in ['traefik','policy-denied']:
        k('create','namespace',ns,check=False)
        k('-n',ns,'run','policy-client','--image=busybox:1.37.0','--restart=Never','--','sleep','600')
        k('-n',ns,'wait','pod/policy-client','--for=condition=Ready','--timeout=180s')
    for app,port,path in [('audiobookshelf',80,'/healthcheck'),('uptime-kuma',3001,'/')]:
        service=json.loads(k('-n',app,'get','service',app,'-o','json').stdout)
        url=f"http://{service['spec']['clusterIP']}:{port}{path}"
        allowed=k('-n','traefik','exec','policy-client','--','wget','-q','-T','8','-O','/dev/null',url,check=False)
        assert allowed.returncode==0,allowed.stderr
        denied=k('-n','policy-denied','exec','policy-client','--','wget','-q','-T','3','-O','/dev/null',url,check=False)
        assert denied.returncode!=0,'NetworkPolicy failed to deny '+app
        passed(app+' NetworkPolicy allow/deny')
    for ns in ['traefik','policy-denied']:
        k('-n',ns,'delete','pod','policy-client','--wait=false')
finally:
    (work/'assertions.json').write_text(json.dumps(results,indent=2)+'\n')
