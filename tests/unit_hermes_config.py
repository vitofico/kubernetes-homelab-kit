"""Configuration and rendered permission boundaries for optional Hermes and FTP."""
import copy
import importlib.util
import ipaddress
from pathlib import Path
import shutil
import tempfile
import unittest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hermes_config_lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class HermesConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'site.yaml'
        self.cfg = yaml.safe_load((ROOT / 'config/site.example.yaml').read_text())
        self.cfg.update(profile='server', domain='lab.example.net', applications=['hermes-agent'])
        self.cfg['images'] = {'IMAGE_HERMES_AGENT': 'registry.example.test/hermes:operator'}
        self.cfg['hermes']['api_cidrs'] = ['192.0.2.1/32']

    def read(self):
        self.path.write_text(yaml.safe_dump(self.cfg))
        return lab.config(self.path)

    def rendered(self, cfg):
        docs = list(yaml.safe_load_all((ROOT / 'applications/hermes-agent/resources.yaml').read_text()))
        docs.append({'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'namespace': 'audiobookshelf', 'name': 'audiobookshelf'}})
        return lab.hermes.augment(cfg, docs, ROOT)

    def test_legacy_sites_get_disabled_defaults(self):
        self.cfg['applications'] = ['traefik']
        self.cfg.pop('hermes')
        self.assertEqual(self.read()['hermes'], lab.hermes.DEFAULTS)

    def test_partial_settings_get_defaults_and_unknown_keys_are_rejected(self):
        self.cfg['hermes'] = {'api_cidrs': ['192.0.2.1/32']}
        self.assertEqual(self.read()['hermes']['telegram'], {'enabled': False, 'allowed_users': []})
        self.assertEqual(self.read()['hermes']['api_ports'], [443, 6443])
        self.cfg['hermes']['extra'] = True
        with self.assertRaises(lab.Error):
            self.read()

    def test_api_cidrs_require_distinct_exact_hosts_when_agent_is_selected(self):
        for value in ([], ['192.0.2.0/24'], ['2001:db8::/64'], ['::ffff:192.0.2.1/128'], ['not-an-ip'], ['192.0.2.1/32'] * 2, '192.0.2.1/32'):
            self.cfg['hermes']['api_cidrs'] = value
            with self.subTest(value=value), self.assertRaises(lab.Error):
                self.read()
        self.cfg['hermes']['api_cidrs'] = ['192.0.2.1/32', '2001:db8::1/128']
        self.assertEqual(self.read()['hermes']['api_cidrs'], self.cfg['hermes']['api_cidrs'])

    def test_api_ports_reject_invalid_or_ambiguous_values(self):
        for ports in ([], [443, 443], [0], [65536], [True], [443.0], ['443'], '443', [[443]]):
            self.cfg['hermes']['api_ports'] = ports
            with self.subTest(ports=ports), self.assertRaises(lab.Error):
                self.read()

    def test_ipv6_exceptions_use_strict_nonmapped_subnets_and_cover_translation_ranges(self):
        docs = self.rendered(self.read())
        policy = next(obj for obj in docs if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == 'hermes-agent-egress')
        public = next(rule['to'][0]['ipBlock'] for rule in policy['spec']['egress']
                      if rule['to'][0].get('ipBlock', {}).get('cidr') == '::/0')
        networks = [ipaddress.ip_network(cidr) for cidr in public['except']]
        self.assertTrue(all(net.version == 6 and net.prefixlen > 0 and net.subnet_of(ipaddress.ip_network('::/0'))
                            and net.network_address.ipv4_mapped is None for net in networks))
        covering = ipaddress.ip_network('::/80')
        self.assertIn(covering, networks)
        self.assertTrue(ipaddress.ip_network('::ffff:0:0/96').subnet_of(covering))
        self.assertIn(ipaddress.ip_network('64:ff9b::/96'), networks)
        self.assertIn(ipaddress.ip_network('64:ff9b:1::/48'), networks)

    def test_microk8s_api_port_is_permitted_for_agent_and_broker_only_at_api_hosts(self):
        self.cfg['hermes']['api_ports'] = [443, 16443]
        self.cfg['hermes']['repair_targets'] = [{'namespace': 'audiobookshelf', 'deployment': 'audiobookshelf'}]
        docs = self.rendered(self.read())
        for name in ('hermes-agent-egress', 'hermes-repair-egress'):
            policy = next(obj for obj in docs if obj['kind'] == 'NetworkPolicy' and obj['metadata']['name'] == name)
            api = next(rule for rule in policy['spec']['egress'] if rule.get('to') == [{'ipBlock': {'cidr': '192.0.2.1/32'}}])
            self.assertEqual(api['ports'], [{'port': 443, 'protocol': 'TCP'}, {'port': 16443, 'protocol': 'TCP'}])
            for rule in policy['spec']['egress']:
                if any(port['port'] == 16443 for port in rule['ports']):
                    self.assertEqual(rule['to'], [{'ipBlock': {'cidr': '192.0.2.1/32'}}])

    def test_targets_are_exact_unique_nonprivileged_deployments(self):
        target = {'namespace': 'audiobookshelf', 'deployment': 'audiobookshelf'}
        for targets in ([dict(target, extra='ignored')], [dict(target, namespace='agent')],
                        [dict(target, namespace='kube-system')], [dict(target, deployment='../other')],
                        [target, target]):
            self.cfg['hermes']['repair_targets'] = targets
            with self.subTest(targets=targets), self.assertRaises(lab.Error):
                self.read()
        self.cfg['hermes']['repair_targets'] = [dict(target, deployment='not-rendered')]
        with self.assertRaises(ValueError):
            self.rendered(self.read())

    def test_telegram_ids_and_secret_are_explicitly_gated(self):
        for users in ([], ['anyone'], [True], [123, '123'], [0], ['-1']):
            self.cfg['hermes']['telegram'] = {'enabled': True, 'allowed_users': users}
            with self.subTest(users=users), self.assertRaises(lab.Error):
                self.read()
        self.cfg['hermes']['telegram'] = {'enabled': True, 'allowed_users': [123, '456']}
        cfg = self.read()
        requirements = lab.required_secrets(cfg)['hermes-agent']
        self.assertIn('TELEGRAM_BOT_TOKEN', requirements['hermes-agent-secrets'])
        docs = self.rendered(cfg)
        agent = next(obj for obj in docs if obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'hermes-agent')
        env = {item['name']: item for item in agent['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['TELEGRAM_ALLOWED_USERS']['value'], '123,456')
        self.assertEqual(env['TELEGRAM_BOT_TOKEN']['valueFrom']['secretKeyRef']['name'], 'hermes-agent-secrets')

    def test_empty_targets_create_no_broker_resources_or_credentials(self):
        cfg = self.read()
        docs = self.rendered(cfg)
        self.assertFalse(any(obj['metadata']['name'] == 'hermes-repair' for obj in docs))
        self.assertNotIn('hermes-repair-credentials', lab.required_secrets(cfg)['hermes-agent'])
        self.assertNotIn('TELEGRAM_BOT_TOKEN', lab.required_secrets(cfg)['hermes-agent']['hermes-agent-secrets'])
        agent = next(obj for obj in docs if obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'hermes-agent')
        self.assertFalse(any(item['name'] in ('REPAIR_URL', 'REPAIR_TOKEN', 'TELEGRAM_BOT_TOKEN') for item in agent['spec']['template']['spec']['containers'][0]['env']))

    def test_repair_uses_separate_scoped_patch_only_identity(self):
        self.cfg['hermes']['repair_targets'] = [{'namespace': 'audiobookshelf', 'deployment': 'audiobookshelf'}]
        cfg = self.read()
        docs = self.rendered(cfg)
        role = next(obj for obj in docs if obj['kind'] == 'Role')
        self.assertEqual(role['metadata']['namespace'], 'audiobookshelf')
        self.assertEqual(role['rules'], [{'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': ['audiobookshelf'], 'verbs': ['patch']}])
        binding = next(obj for obj in docs if obj['kind'] == 'RoleBinding')
        self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'namespace': 'agent', 'name': 'hermes-repair'}])
        broker = next(obj for obj in docs if obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'hermes-repair')
        self.assertEqual(broker['apiVersion'], 'apps/v1')
        pod = broker['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], 'hermes-repair')
        self.assertEqual(pod['containers'][0]['image'], 'python:3.13-alpine')
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(pod['containers'][0]['securityContext']['runAsUser'], 65532)
        self.assertEqual(lab.required_secrets(cfg)['hermes-agent']['hermes-repair-credentials'], ['REPAIR_TOKEN'])
        self.assertIn('REPAIR_TOKEN', lab.INTERNAL_SECRET_KEYS)
        readonly = next(obj for obj in docs if obj['kind'] == 'ClusterRole')
        self.assertTrue(all(set(rule['verbs']) <= {'get', 'list', 'watch'} for rule in readonly['rules']))
        self.assertFalse(any(resource in ('secrets', 'pods/log', 'pods/exec') for rule in readonly['rules'] for resource in rule['resources']))
        scoped = [obj for obj in docs if obj['metadata']['name'] == 'hermes-repair' or obj['metadata']['name'].startswith('hermes-repair-')]
        self.assertEqual({obj['kind'] for obj in scoped}, {'Deployment', 'Service', 'ServiceAccount', 'Role', 'RoleBinding', 'NetworkPolicy'})
        self.assertTrue(all(obj['metadata']['labels'] == lab.hermes.REPAIR_LABELS for obj in scoped))
        shared = [obj for obj in docs if obj['metadata']['name'] in ('hermes-ops-code', 'hermes-ops-skill', 'hermes-agent')]
        self.assertTrue(all(obj['metadata'].get('labels', {}).get('app.kubernetes.io/component') != 'hermes-repair' for obj in shared))

    def test_home_initialization_webhook_and_egress_boundaries(self):
        cfg = self.read()
        docs = self.rendered(cfg)
        agent = next(obj for obj in docs if obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'hermes-agent')
        pod = agent['spec']['template']['spec']
        self.assertEqual(pod['securityContext']['fsGroup'], 10000)
        prepare, copy_config = pod['initContainers']
        self.assertNotIn('-R', prepare['args'][0])
        self.assertIn('chown 10000:10000 /opt/data /opt/data/.hermes;', prepare['args'][0])
        self.assertEqual(copy_config['command'], ['sh', '-ec'])
        self.assertEqual(copy_config['securityContext']['runAsUser'], 10000)
        env = {item['name']: item for item in pod['containers'][0]['env']}
        self.assertEqual(env['HOME']['value'], '/opt/data')
        self.assertEqual(env['HERMES_HOME']['value'], '/opt/data/.hermes')
        self.assertEqual(env['HERMES_WRITE_SAFE_ROOT']['value'], '/opt/data')
        self.assertEqual(env['WEBHOOK_SECRET']['valueFrom'], env['HERMES_WEBHOOK_SECRET']['valueFrom'])
        cm = next(obj for obj in docs if obj['kind'] == 'ConfigMap' and obj['metadata']['name'] == 'hermes-agent-config')
        webhook = yaml.safe_load(cm['data']['config.yaml'])['platforms']['webhook']
        self.assertTrue(webhook['enabled'])
        self.assertEqual(webhook['extra']['host'], '0.0.0.0')
        self.assertEqual(webhook['extra']['routes']['uptime-kuma'], {'toolsets': [], 'deliver': 'log'})
        policies = [obj for obj in docs if obj['kind'] == 'NetworkPolicy']
        self.assertFalse(any(obj['metadata']['name'] == 'allow-egress-mediaserver' for obj in policies))
        egress = next(obj for obj in policies if obj['metadata']['name'] == 'hermes-agent-egress')['spec']['egress']
        api = next(rule for rule in egress if rule.get('to') == [{'ipBlock': {'cidr': '192.0.2.1/32'}}])
        self.assertEqual({port['port'] for port in api['ports']}, {443, 6443})
        public = next(rule for rule in egress if rule['to'][0].get('ipBlock', {}).get('cidr') == '0.0.0.0/0')
        self.assertTrue({'10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '192.0.2.1/32'} <= set(public['to'][0]['ipBlock']['except']))

    @unittest.skipUnless(shutil.which('kustomize'), 'kustomize required')
    def test_full_and_per_app_render_have_consistent_repair_objects(self):
        self.cfg['applications'].append('audiobookshelf')
        self.cfg['hermes']['repair_targets'] = [{'namespace': 'audiobookshelf', 'deployment': 'audiobookshelf'}]
        cfg = self.read()
        docs = lab.documents(cfg)
        part = copy.deepcopy(cfg)
        part['applications'] = ['hermes-agent']
        subset = lab.documents(part, validate_targets=False)
        broker = lambda objects: next(obj for obj in objects if obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'hermes-repair')
        self.assertEqual(broker(docs), broker(subset))
        with self.assertRaises(lab.Error):
            lab.documents(part)

    def test_ftp_requires_separate_valid_lan_pool_address(self):
        self.cfg['applications'] = ['ftp-camera']
        with self.assertRaises(lab.Error):
            self.read()
        for address in ('192.0.2.10', '192.0.2.20', '192.0.2.21', '198.51.100.11', 'invalid', 3221225995):
            self.cfg['network']['ftp_ip'] = address
            with self.subTest(address=address), self.assertRaises(lab.Error):
                self.read()
        self.cfg['network']['ftp_ip'] = '192.0.2.11'
        self.assertEqual(self.read()['network']['ftp_ip'], '192.0.2.11')

    @unittest.skipUnless(shutil.which('kustomize'), 'kustomize required')
    def test_ftp_control_and_passive_address_use_same_configured_ip(self):
        self.cfg['applications'] = ['ftp-camera']
        self.cfg['network']['ftp_ip'] = '192.0.2.11'
        docs = lab.documents(self.read())
        service = next(obj for obj in docs if obj['kind'] == 'Service')
        deployment = next(obj for obj in docs if obj['kind'] == 'Deployment')
        env = {item['name']: item for item in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(service['spec']['loadBalancerIP'], '192.0.2.11')
        self.assertEqual(env['PASV_ADDRESS']['value'], '192.0.2.11')


if __name__ == '__main__':
    unittest.main()
