#!/usr/bin/env python3
"""One explicit cluster inspection or broker-authorized deployment restart."""
import argparse
from http.client import HTTPException
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, ProxyHandler, Request

from repair_server import NoRedirect, RequestError, deployment_path, exact_target, kube_request


def restart(namespace, deployment):
    try:
        url = urlsplit(os.environ['REPAIR_URL'])
        token = os.environ['REPAIR_TOKEN']
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.path not in ('', '/') or url.query or url.fragment or not token):
            raise ValueError('Invalid repair endpoint')
        request = Request(url.geturl().rstrip('/') + '/restart',
                          data=json.dumps({'namespace': namespace, 'deployment': deployment}).encode(),
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
        with build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context())).open(request, timeout=15) as response:
            if response.status != 202:
                raise ValueError('Unexpected repair response')
        return {'status': 'Restart requested; use status to inspect readiness'}
    except (KeyError, ValueError, OSError, HTTPException, HTTPError, URLError):
        raise RequestError('Repair request failed; check broker availability, approved targets, and cooldown') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'restart'])
    parser.add_argument('namespace')
    parser.add_argument('deployment')
    args = parser.parse_args(argv)
    try:
        namespace, deployment = exact_target({'namespace': args.namespace, 'deployment': args.deployment})
        if args.action == 'restart':
            result = restart(namespace, deployment)
        else:
            obj = kube_request('GET', deployment_path(namespace, deployment))
            status = obj.get('status', {})
            result = {'namespace': namespace, 'deployment': deployment,
                      'desiredReplicas': obj.get('spec', {}).get('replicas', 1),
                      'readyReplicas': status.get('readyReplicas', 0),
                      'updatedReplicas': status.get('updatedReplicas', 0),
                      'observedGeneration': status.get('observedGeneration', 0),
                      'generation': obj.get('metadata', {}).get('generation', 0),
                      'conditions': [{'type': c.get('type'), 'status': c.get('status')} for c in status.get('conditions', [])]}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RequestError, ValueError):
        print('Cluster operation failed; check the target, service account, and repair broker configuration.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
