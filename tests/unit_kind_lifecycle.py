"""Exercise shell lifecycle ownership without creating Docker resources."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class KindLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for relative in ('tests/test-kind.sh', 'tests/kind/bootstrap.sh'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / relative, target)
        binary = self.root / 'bin'
        binary.mkdir()
        for command in ('docker', 'kubectl', 'curl', 'sops', 'age-keygen'):
            target = binary / command
            target.write_text('#!/bin/sh\nexit 0\n')
            target.chmod(0o755)
        kind = binary / 'kind'
        kind.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_WORK/commands"
case "$1 $2" in
  'get clusters') test ! -f "$FAKE_WORK/existing" || cat "$FAKE_WORK/existing" ;;
  'create cluster')
    touch "$FAKE_WORK/creating"
    if test -f "$FAKE_WORK/block"; then
      while test ! -f "$FAKE_WORK/release"; do sleep 0.02; done
    fi
    exit 1 ;;
  'delete cluster')
    test -f "$KIND_TEST_LOCK/owner" || exit 2
    echo delete-with-lock >> "$FAKE_WORK/commands" ;;
  *) exit 2 ;;
esac
''')
        kind.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                        TMPDIR=str(self.root), FAKE_WORK=str(self.root))
        self.env.pop('TEST_CLUSTER_NAME', None)
        self.env.pop('KEEP_KIND_ON_FAILURE', None)
        self.command = [str(self.root / 'tests/test-kind.sh')]

    def invoke(self, name=None):
        env = dict(self.env)
        if name:
            env['TEST_CLUSTER_NAME'] = name
        return subprocess.run(self.command, env=env, capture_output=True, text=True, timeout=10)

    def test_explicit_name_is_locked_through_partial_create_cleanup(self):
        (self.root / 'block').touch()
        env = dict(self.env, TEST_CLUSTER_NAME='same-test-name')
        owner = subprocess.Popen(self.command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / 'creating').exists():
                self.assertIsNone(owner.poll(), 'Owner exited before attempting cluster creation')
                self.assertLess(time.monotonic(), deadline, 'Owner did not reach fake create')
                time.sleep(0.02)
            contender = self.invoke('same-test-name')
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn('Another test run owns cluster name', contender.stderr)
            commands = (self.root / 'commands').read_text()
            self.assertEqual(commands.count('get clusters'), 1)
            self.assertNotIn('delete cluster', commands)
        finally:
            (self.root / 'release').touch()
            owner.communicate(timeout=10)
        self.assertNotEqual(owner.returncode, 0)
        commands = (self.root / 'commands').read_text()
        self.assertEqual(commands.count('delete cluster'), 1)
        self.assertIn('delete-with-lock', commands)
        self.assertFalse((self.root / 'kubernetes-homelab-kit-kind-locks/same-test-name.lock').exists())

    def test_existing_cluster_is_never_marked_owned_or_deleted(self):
        (self.root / 'existing').write_text('already-present\n')
        result = self.invoke('already-present')
        self.assertNotEqual(result.returncode, 0)
        commands = (self.root / 'commands').read_text()
        self.assertEqual(commands, 'get clusters\n')
        self.assertFalse(list(self.root.glob('local/test-work/*/created-cluster')))

    def test_default_names_are_unique_and_partial_create_is_cleaned(self):
        for _ in range(2):
            self.assertNotEqual(self.invoke().returncode, 0)
        commands = (self.root / 'commands').read_text().splitlines()
        creations = [line.split('--name ', 1)[1].split()[0] for line in commands if line.startswith('create cluster')]
        self.assertEqual(len(creations), 2)
        self.assertEqual(len(set(creations)), 2)
        self.assertTrue(all(name.startswith('kubernetes-homelab-kit-review-') for name in creations))
        self.assertEqual(sum(line.startswith('delete cluster') for line in commands), 2)


if __name__ == '__main__':
    unittest.main()
