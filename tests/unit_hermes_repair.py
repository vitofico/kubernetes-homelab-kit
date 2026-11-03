"""Exercise the opt-in repair boundary with local HTTP and mocked Kubernetes APIs."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'integrations/hermes'
spec = importlib.util.spec_from_file_location('repair_server', SOURCE / 'repair_server.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)
with patch.dict(sys.modules, {'repair_server': repair}):
    spec = importlib.util.spec_from_file_location('hermes_ops', SOURCE / 'ops.py')
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)


class RepairBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock(return_value={})
        self.now = 100
        self.target = {'namespace': 'workloads', 'deployment': 'example-app'}
        self.broker = repair.RepairBroker('private-repair-token', [self.target], api=self.api, clock=lambda: self.now)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), repair.RepairHandler)
        self.server.broker = self.broker
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, value=None, token='private-repair-token', path='/restart', method='POST', raw=None):
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            body = raw if raw is not None else json.dumps(self.target if value is None else value)
            headers = {'Content-Type': 'application/json'}
            if token is not None:
                headers['Authorization'] = 'Bearer ' + token
            client.request(method, path, body=body if method != 'GET' else None, headers=headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_health_is_only_unauthenticated_endpoint(self):
        self.assertEqual(self.request(method='GET', path='/health', token=None), (200, {'status': 'ok'}))
        self.assertEqual(self.request(method='GET', path='/restart', token=None)[0], 404)
        for token in (None, '', 'wrong-token'):
            self.assertEqual(self.request(token=token)[0], 401)
        self.api.assert_not_called()

    def test_unknown_target_extra_fields_and_duplicate_fields_never_reach_api(self):
        for value in ({'namespace': 'other', 'deployment': 'example-app'},
                      dict(self.target, patch={'spec': {'replicas': 0}}),
                      {'namespace': 'workloads', 'deployment': '../secrets'}, [], None):
            if value is None:
                status, _ = self.request(raw='null')
            else:
                status, _ = self.request(value)
            self.assertIn(status, (400, 403))
        self.assertEqual(self.request(raw='{"namespace":"workloads","namespace":"other","deployment":"example-app"}')[0], 400)
        self.api.assert_not_called()

    def test_fixed_patch_and_cooldown(self):
        self.assertEqual(self.request()[0], 202)
        method, path, patch_body = self.api.call_args.args
        self.assertEqual(method, 'PATCH')
        self.assertEqual(path, '/apis/apps/v1/namespaces/workloads/deployments/example-app')
        annotations = patch_body['spec']['template']['metadata']['annotations']
        self.assertEqual(set(annotations), {'kubernetes-homelab-kit/restartedAt'})
        self.assertTrue(annotations['kubernetes-homelab-kit/restartedAt'].endswith('Z'))
        self.assertEqual(patch_body, {'spec': {'template': {'metadata': {'annotations': annotations}}}})
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(self.api.call_count, 1)
        self.now += 60
        self.assertEqual(self.request()[0], 202)
        self.assertEqual(self.api.call_count, 2)

    def test_api_errors_are_redacted_and_cooldown_still_applies(self):
        self.api.side_effect = repair.RequestError('private-upstream-response private-repair-token')
        output = io.StringIO()
        with redirect_stderr(output):
            status, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn('private-upstream-response', json.dumps(body) + output.getvalue())
        self.assertNotIn('private-repair-token', json.dumps(body) + output.getvalue())
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(self.api.call_count, 1)

    def test_body_limit_and_unknown_routes(self):
        self.assertEqual(self.request(raw='x' * (repair.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request(raw='not-json')[0], 400)
        self.assertEqual(self.request(path='/restart?target=other')[0], 404)
        self.api.assert_not_called()

    def test_nonfinite_or_ambiguous_request_framing_is_rejected(self):
        for headers in ({}, {'Content-Length': '-1'}, {'Transfer-Encoding': 'chunked'},
                        {'Content-Length': '10', 'Transfer-Encoding': 'chunked'}):
            client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
            try:
                client.putrequest('POST', '/restart')
                client.putheader('Authorization', 'Bearer private-repair-token')
                for name, value in headers.items():
                    client.putheader(name, value)
                client.endheaders()
                response = client.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                client.close()
        self.api.assert_not_called()

    def test_cooldown_is_scoped_to_each_approved_target(self):
        other = {'namespace': 'workloads', 'deployment': 'other-app'}
        self.server.broker = repair.RepairBroker('private-repair-token', [self.target, other], api=self.api)
        self.assertEqual(self.request()[0], 202)
        self.assertEqual(self.request(other)[0], 202)
        self.assertEqual(self.api.call_count, 2)

    def test_empty_allowlist_cannot_restart(self):
        self.server.broker = repair.RepairBroker('private-repair-token', [], api=self.api)
        self.assertEqual(self.request()[0], 403)
        self.api.assert_not_called()

    def test_no_redirect_and_upstream_errors_do_not_expose_service_token(self):
        self.assertIsNone(repair.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'token').write_text('service-account-private-token')
            opener = Mock()
            opener.open.side_effect = OSError('private-upstream-response service-account-private-token')
            with patch.object(repair, 'SERVICE_ACCOUNT', path), patch.dict(os.environ, {'KUBERNETES_SERVICE_HOST': '10.96.0.1'}), \
                    patch.object(repair.ssl, 'create_default_context'), patch.object(repair, 'build_opener', return_value=opener):
                with self.assertRaises(repair.RequestError) as error:
                    repair.kube_request('PATCH', repair.deployment_path(**self.target), {})
            self.assertNotIn('service-account-private-token', str(error.exception))
            self.assertNotIn('private-upstream-response', str(error.exception))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://10.96.0.1:443/apis/apps/v1/namespaces/workloads/deployments/example-app')
            self.assertEqual(request.get_header('Content-type'), 'application/merge-patch+json')
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)

    def test_helper_status_is_one_read_and_omits_raw_workload_values(self):
        obj = {'spec': {'replicas': 2, 'template': {'spec': {'containers': [{'env': 'private-workload-value'}]}}},
               'status': {'readyReplicas': 1, 'updatedReplicas': 2, 'conditions': [{'type': 'Available', 'status': 'False', 'message': 'private-message'}]},
               'metadata': {'generation': 4}}
        output = io.StringIO()
        with patch.object(ops, 'kube_request', return_value=obj) as api, redirect_stdout(output):
            result = ops.main(['status', 'workloads', 'example-app'])
        self.assertEqual(result, 0)
        api.assert_called_once_with('GET', repair.deployment_path(**self.target))
        self.assertEqual(json.loads(output.getvalue())['readyReplicas'], 1)
        self.assertNotIn('private-', output.getvalue())

    def test_helper_restart_uses_broker_and_never_calls_kubernetes(self):
        output = io.StringIO()
        with patch.object(ops, 'restart', return_value={'status': 'requested'}) as restart, \
                patch.object(ops, 'kube_request') as api, redirect_stdout(output):
            self.assertEqual(ops.main(['restart', 'workloads', 'example-app']), 0)
        restart.assert_called_once_with('workloads', 'example-app')
        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
