"""Exercise scoped revocation ordering and failure behavior without a cluster."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RevokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.kube = self.work / 'test-kubeconfig'
        self.kube.touch()
        self.log = self.work / 'commands.jsonl'
        binary = self.work / 'kubectl'
        binary.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['REVOKE_TEST_LOG']).open('a') as output:
    output.write(json.dumps(args)+'\\n')
if os.environ.get('REVOKE_FAIL') and args[2:4]==['get','rolebindings']:
    sys.exit(1)
if 'get' in args:
    print('synthetic-app hermes-repair')
''')
        binary.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.work) + ':' + os.environ['PATH'],
                        REVOKE_TEST_LOG=str(self.log))

    def invoke(self):
        return subprocess.run([str(ROOT / 'scripts/revoke-hermes.sh'), str(self.kube)],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_revocation_removes_grants_before_broker_without_general_prune(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.commands()
        for call in calls:
            self.assertEqual(call[:2], ['--kubeconfig', str(self.kube)])
        deletions = [call for call in calls if 'delete' in call]
        self.assertIn('rolebindings', deletions[0])
        self.assertIn('roles', deletions[1])
        self.assertIn('deployment,service,serviceaccount,networkpolicy,secret', deletions[2])
        self.assertIn('synthetic-app', deletions[0])
        self.assertTrue(all('app.kubernetes.io/component=hermes-repair' in ' '.join(call)
                            for call in calls if 'get' in call or ',' in ' '.join(call)))
        self.assertFalse(any('namespace' in call or 'clusterrole' in call for call in deletions))

    def test_inventory_failure_stops_without_deletion_or_success_claim(self):
        self.env['REVOKE_FAIL'] = '1'
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('delete' in call for call in self.commands()))
        self.assertNotIn('revoked', result.stdout)

    def test_missing_kubeconfig_never_invokes_kubectl(self):
        self.kube.unlink()
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
