#!/usr/bin/env python3
"""Narrow deployment-restart broker. Run under its own restricted service account."""
from datetime import datetime, timezone
import hmac
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request


SERVICE_ACCOUNT = Path('/var/run/secrets/kubernetes.io/serviceaccount')
MAX_BODY = 4096
COOLDOWN = 60
LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'


class RequestError(Exception):
    """A safe, operator-visible error; never include upstream response content."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exact_target(value):
    if (not isinstance(value, dict) or set(value) != {'namespace', 'deployment'}
            or not isinstance(value['namespace'], str)
            or not re.fullmatch(LABEL, value['namespace'])
            or not isinstance(value['deployment'], str)
            or len(value['deployment']) > 253
            or not all(re.fullmatch(LABEL, label) for label in value['deployment'].split('.'))):
        raise ValueError('Expected exactly namespace and deployment DNS names')
    return value['namespace'], value['deployment']


def deployment_path(namespace, deployment):
    return '/apis/apps/v1/namespaces/' + quote(namespace, safe='') + '/deployments/' + quote(deployment, safe='')


def kube_request(method, path, payload=None):
    """Verified service-account HTTPS, bounded responses, no credential redirects."""
    try:
        host = os.environ['KUBERNETES_SERVICE_HOST']
        try:
            address = ipaddress.ip_address(host)
            host = '[' + host + ']' if address.version == 6 else host
        except ValueError:
            if len(host) > 253 or not all(re.fullmatch(LABEL, part) for part in host.split('.')):
                raise ValueError('Invalid Kubernetes service host') from None
        port = int(os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', os.environ.get('KUBERNETES_SERVICE_PORT', '443')))
        if not 1 <= port <= 65535:
            raise ValueError('Invalid Kubernetes service port')
        context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / 'ca.crt'))
        token = (SERVICE_ACCOUNT / 'token').read_text().strip()
        request = Request('https://' + host + ':' + str(port) + path,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={'Authorization': 'Bearer ' + token,
                                   'Content-Type': 'application/merge-patch+json' if method == 'PATCH' else 'application/json'},
                          method=method)
        with build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context)).open(request, timeout=10) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError('Oversized Kubernetes response')
            return json.loads(body)
    except (OSError, KeyError, ValueError, HTTPException, HTTPError, URLError):
        raise RequestError('Kubernetes API request failed; response details withheld') from None


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON field')
        value[key] = item
    return value


class RepairBroker:
    def __init__(self, token, targets, api=kube_request, clock=time.monotonic):
        if not isinstance(token, str) or not token:
            raise ValueError('REPAIR_TOKEN is required')
        if not isinstance(targets, list):
            raise ValueError('REPAIR_TARGETS must be a JSON list')
        self.token = token.encode()
        self.targets = {exact_target(target) for target in targets}
        self.api, self.clock = api, clock
        self.last_restart = {}
        self.lock = threading.Lock()

    def restart(self, authorization, value):
        if not hmac.compare_digest(authorization.encode(), b'Bearer ' + self.token):
            return 401, {'error': 'Unauthorized'}
        try:
            target = exact_target(value)
        except ValueError:
            return 400, {'error': 'Expected exactly namespace and deployment DNS names'}
        if target not in self.targets:
            return 403, {'error': 'Target is not approved for repair'}
        with self.lock:
            now = self.clock()
            if target in self.last_restart and now - self.last_restart[target] < COOLDOWN:
                return 429, {'error': 'Target restart cooldown is active'}
            self.last_restart[target] = now
        timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        patch = {'spec': {'template': {'metadata': {'annotations': {'kubernetes-homelab-kit/restartedAt': timestamp}}}}}
        try:
            self.api('PATCH', deployment_path(*target), patch)
        except RequestError:
            return 502, {'error': 'Kubernetes restart request failed; response details withheld'}
        return 202, {'status': 'Restart requested; inspect deployment readiness'}


class RepairHandler(BaseHTTPRequestHandler):
    server_version = 'HermesRepair'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        pass  # Neither request headers/bodies nor upstream errors belong in logs.

    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def do_GET(self):
        self.respond(200, {'status': 'ok'}) if self.path == '/health' else self.respond(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path != '/restart':
            self.respond(404, {'error': 'Not found'})
            return
        authorization = self.headers.get('Authorization', '')
        if not hmac.compare_digest(authorization.encode(), b'Bearer ' + self.server.broker.token):
            self.respond(401, {'error': 'Unauthorized'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]) or self.headers.get('Transfer-Encoding'):
            self.respond(400, {'error': 'One finite Content-Length is required'})
            return
        length = int(lengths[0])
        if length > MAX_BODY:
            self.respond(413, {'error': 'Request body exceeds 4096 bytes'})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')
            value = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, OSError):
            self.respond(400, {'error': 'Invalid JSON request body'})
            return
        status, value = self.server.broker.restart(authorization, value)
        self.respond(status, value)


def main():
    try:
        broker = RepairBroker(os.environ.get('REPAIR_TOKEN', ''), json.loads(os.environ.get('REPAIR_TARGETS', '[]'), object_pairs_hook=unique_object))
        port = int(os.environ.get('REPAIR_PORT', '8080'))
        server = ThreadingHTTPServer(('0.0.0.0', port), RepairHandler)
        server.broker = broker
        server.serve_forever()
    except (ValueError, OSError):
        raise SystemExit('Repair broker could not start; check its configuration') from None


if __name__ == '__main__':
    main()
