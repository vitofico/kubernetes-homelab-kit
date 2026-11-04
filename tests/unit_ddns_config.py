"""DDNS selection, secret and rendered job boundaries; never calls a provider."""
import copy
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ddns_config_lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class DdnsConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'site.yaml'
        self.cfg = yaml.safe_load((ROOT / 'config/site.example.yaml').read_text())
        self.cfg.update(profile='server', domain='lab.example.net', applications=['dynamic-dns-updater'])
        self.cfg['images'] = {'IMAGE_DYNAMIC_DNS_UPDATER': 'registry.example.invalid/updater:test'}
        self.cfg['ddns'] = {'provider': 'desec', 'domains': 'host.example.net'}

    def read(self):
        self.path.write_text(yaml.safe_dump(self.cfg))
        return lab.config(self.path)

    def rendered(self, cfg):
        docs = list(yaml.safe_load_all((ROOT / 'applications/dynamic-dns-updater/resources.yaml').read_text()))
        return lab.ddns.augment(cfg, docs)

    def test_legacy_configuration_gets_disabled_defaults(self):
        self.cfg.update(applications=['argo'], profile='kind')
        self.cfg.pop('ddns')
        cfg = self.read()
        self.assertTrue(cfg['ddns']['suspend'])
        self.assertNotIn('dynamic-dns-updater', lab.required_secrets(cfg))

    def test_each_provider_requires_only_its_external_secret(self):
        for provider, key in lab.ddns.CREDENTIALS.items():
            self.cfg['ddns'].update(provider=provider, hostname='host.example.net')
            cfg = self.read()
            requirements = lab.required_secrets(cfg)
            self.assertEqual(requirements, {'dynamic-dns-updater': {'dynamic-dns-credentials': [key]}})
            job = self.rendered(cfg)[1]
            self.assertTrue(job['spec']['suspend'])
            pod = lab.pod_spec(job)
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
            env = {entry['name']: entry for entry in pod['containers'][0]['env']}
            self.assertEqual(env['DNS_PROVIDER']['value'], provider)
            self.assertEqual(env[key]['valueFrom']['secretKeyRef']['key'], key)
            self.assertNotIn('kind: Secret', yaml.safe_dump_all(self.rendered(cfg)))

    def test_desec_preserves_other_family_and_operator_schedule(self):
        self.cfg['ddns'].update(schedule='0 */2 * * *', suspend=False)
        job = self.rendered(self.read())[1]
        self.assertEqual(job['spec']['schedule'], '0 */2 * * *')
        self.assertFalse(job['spec']['suspend'])
        env = {item['name']: item.get('value') for item in lab.pod_spec(job)['containers'][0]['env']}
        self.assertEqual(env['DESEC_IPV4'], 'auto')
        self.assertEqual(env['DESEC_IPV6'], 'preserve')

    def test_invalid_configuration_fails_before_render(self):
        cases = [('provider', []), ('provider', 'other'), ('schedule', '@reboot'),
                 ('schedule', '* * * * *\n'), ('schedule', '60 * * * *'),
                 ('schedule', '*/0 * * * *'), ('schedule', '* 4-2 * * *'),
                 ('suspend', 'false'), ('domains', ''), ('domains', 'a.example.net, bad'),
                 ('ipv4', ''), ('ipv4', '2001:db8::1'), ('ipv6', '192.0.2.1'), ('extra', True)]
        original = copy.deepcopy(self.cfg['ddns'])
        for key, value in cases:
            self.cfg['ddns'] = dict(original, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(lab.Error):
                self.read()

    def test_external_effects_are_excluded_from_kind(self):
        self.cfg['profile'] = 'kind'
        with self.assertRaisesRegex(lab.Error, 'not supported by the kind profile'):
            self.read()

    @unittest.skipUnless(shutil.which('kustomize'), 'kustomize required')
    def test_real_render_replaces_image_and_uses_only_secret_references(self):
        cfg = self.read()
        rendered = lab.documents(cfg)
        job = next(item for item in rendered if item['kind'] == 'CronJob')
        self.assertEqual(lab.pod_spec(job)['containers'][0]['image'], self.cfg['images']['IMAGE_DYNAMIC_DNS_UPDATER'])
        self.assertNotIn('__IMAGE_', yaml.safe_dump_all(rendered))
        self.assertNotIn('kind: Secret', yaml.safe_dump_all(rendered))

    @unittest.skipUnless(shutil.which('kustomize'), 'kustomize required')
    def test_reviewed_catalog_image_renders_without_an_override(self):
        self.cfg['images'] = {}
        docs = lab.documents(self.read())
        job = next(item for item in docs if item['kind'] == 'CronJob')
        image = lab.pod_spec(job)['containers'][0]['image']
        self.assertRegex(image, r'^ghcr\.io/vitofico/dynamic-dns-updater@sha256:[0-9a-f]{64}$')

    def test_missing_credentials_fail_doctor(self):
        self.read()
        with patch.object(lab.shutil, 'which', return_value='/synthetic/tool'), patch.object(lab, 'secret_store', return_value={}):
            with self.assertRaisesRegex(lab.Error, 'External credentials missing'):
                lab.doctor(type('Args', (), {'config': str(self.path)})())


if __name__ == '__main__':
    unittest.main()
