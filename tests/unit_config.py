import argparse
import copy
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

spec = importlib.util.spec_from_file_location('lab', Path(__file__).resolve().parents[1] / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'applications' / 'sample').mkdir(parents=True)
        self.cat = {'apps': {'sample': {'path': 'applications/sample', 'namespace': 'sample', 'kind_supported': True, 'secrets': {}}}}
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        self.cfg = yaml.safe_load((lab.ROOT / 'config/site.example.yaml').read_text())
        self.cfg['applications'] = ['sample']
        self.path = self.root / 'local/site.yaml'
        self.path.parent.mkdir()
        self.write()
        self.patcher = patch.object(lab, 'ROOT', self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def write(self):
        self.path.write_text(yaml.safe_dump(self.cfg))

    def test_accepts_example_with_catalog(self):
        self.assertEqual(lab.config(self.path)['profile'], 'kind')

    def test_selected_catalog_image_defaults_preserve_both_override_spellings(self):
        marker = '__IMAGE_SAMPLE__'
        default = 'registry.example.invalid/sample@sha256:' + '0' * 64
        self.cat['apps']['sample'].update(required_images={marker: 'reviewed image'}, default_images={marker: default})
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        self.assertEqual(lab.config(self.path)['images'][marker], default)
        for key in (marker, marker.strip('_')):
            self.cfg['images'] = {key: 'registry.example.invalid/override:review'}
            self.write()
            images = lab.config(self.path)['images']
            self.assertEqual(images, self.cfg['images'])

    def test_rejects_unknown_inputs_and_apps(self):
        for field, value in [('unexpected', 'x'), ('applications', ['missing']), ('profile', 'other'), ('domain', 'bad/domain')]:
            cfg = copy.deepcopy(self.cfg)
            cfg[field] = value
            self.path.write_text(yaml.safe_dump(cfg))
            with self.subTest(field=field), self.assertRaises(lab.Error):
                lab.config(self.path)

    def test_rejects_invalid_network_and_path(self):
        for section, key, value in [('network', 'lan_cidr', 'not-a-network'), ('network', 'loadbalancer_ip', '203.0.113.1'), ('storage', 'nfs_root', '/exports/../private')]:
            cfg = copy.deepcopy(self.cfg)
            cfg[section][key] = value
            self.path.write_text(yaml.safe_dump(cfg))
            with self.subTest(field=key), self.assertRaises(lab.Error):
                lab.config(self.path)

    def test_ip_and_cidr_fields_reject_yaml_nonstring_values_before_rendering(self):
        for section, key in (('storage', 'nfs_server'), ('network', 'lan_cidr'), ('network', 'loadbalancer_ip')):
            for value in (False, True, 0, 3221225995, None, [], {}):
                cfg = copy.deepcopy(self.cfg)
                cfg[section][key] = value
                self.path.write_text(yaml.safe_dump(cfg))
                with self.subTest(section=section, field=key, value=value), self.assertRaisesRegex(lab.Error, 'must be a string'):
                    lab.config(self.path)

    def test_cluster_target_is_explicit(self):
        with self.assertRaises(lab.Error):
            lab.kube(argparse.Namespace(kubeconfig=''))
        kube = self.root / 'kubeconfig'
        kube.write_text('test')
        self.assertEqual(lab.kube(argparse.Namespace(kubeconfig=str(kube)))[1:], ['--kubeconfig', str(kube.resolve())])

    def test_rollout_timeout_defaults_to_five_minutes_without_startup_probe(self):
        obj = {'kind': 'Deployment', 'spec': {'template': {'spec': {'containers': [
            {'name': 'app', 'readinessProbe': {'initialDelaySeconds': 10, 'periodSeconds': 5}}
        ]}}}}
        self.assertEqual(lab.rollout_timeout(obj), 300)

    def test_rollout_timeout_honors_actual_authentik_startup_budgets(self):
        source = Path(__file__).resolve().parents[1] / 'applications/authentik/resources.yaml'
        deployments = {obj['metadata']['name']: obj for obj in yaml.safe_load_all(source.read_text())
                       if obj and obj.get('kind') == 'Deployment'}
        self.assertEqual(lab.rollout_timeout(deployments['authentik-server']), 720)
        self.assertEqual(lab.rollout_timeout(deployments['authentik-worker']), 1320)

    def test_rollout_timeout_uses_largest_parallel_container_window(self):
        obj = {'kind': 'Deployment', 'spec': {'template': {'spec': {'containers': [
            {'name': 'first', 'startupProbe': {'initialDelaySeconds': 5, 'failureThreshold': 20, 'periodSeconds': 10}},
            {'name': 'second', 'startupProbe': {'initialDelaySeconds': 7, 'failureThreshold': 10, 'periodSeconds': 5, 'timeoutSeconds': 30}}
        ]}}}}
        self.assertEqual(lab.rollout_timeout(obj), 427)

    def test_certmanager_api_wait_retries_webhook_propagation(self):
        outcomes = [SimpleNamespace(returncode=1, stderr='Internal error: failed calling webhook: connection refused'), SimpleNamespace(returncode=0, stderr='')]
        with patch.object(lab.subprocess, 'run', side_effect=outcomes) as invoke, patch.object(lab.time, 'sleep'):
            lab.wait_certmanager(['kubectl', '--kubeconfig', 'explicit'])
        self.assertEqual(invoke.call_count, 2)
        self.assertIn('--dry-run=server', invoke.call_args.args[0])

    def test_certmanager_api_wait_fails_permanent_rejection(self):
        with patch.object(lab.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='Forbidden: RBAC denied')) as invoke, self.assertRaises(lab.Error):
            lab.wait_certmanager(['kubectl'])
        self.assertEqual(invoke.call_count, 1)

    def test_certmanager_api_wait_retries_observed_transport_timeouts(self):
        for diagnostic in (
            'Unable to connect to the server: context deadline exceeded',
            'Unable to connect to the server: dial tcp 192.0.2.1:6443: i/o timeout',
            'Unable to connect to the server: net/http: TLS handshake timeout',
        ):
            outcomes = [SimpleNamespace(returncode=1, stderr=diagnostic), SimpleNamespace(returncode=0, stderr='')]
            with self.subTest(diagnostic=diagnostic), patch.object(lab.subprocess, 'run', side_effect=outcomes) as invoke, patch.object(lab.time, 'sleep'):
                lab.wait_certmanager(['kubectl', '--kubeconfig', 'explicit'])
                self.assertEqual(invoke.call_count, 2)

    def test_certmanager_api_wait_has_deadline(self):
        with patch.object(lab.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='failed calling webhook')), patch.object(lab.time, 'monotonic', side_effect=[0, 121]), self.assertRaises(lab.Error):
            lab.wait_certmanager(['kubectl'], timeout=120)

    def test_unresolved_markers_fail(self):
        manifest = 'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: sample}\ndata: {unknown: __MISSING__}\n'
        with patch.object(lab, 'run', return_value=manifest), self.assertRaises(lab.Error):
            lab.documents(self.cfg)

    def test_kind_profile_storage_and_ingress(self):
        manifest = '''apiVersion: v1
kind: PersistentVolume
metadata: {name: sample}
spec:
  nfs: {server: __NFS_SERVER__, path: __NFS_ROOT__/data}
  mountOptions: [soft]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sample
  annotations: {cert-manager.io/cluster-issuer: production, traefik.ingress.kubernetes.io/router.tls: 'true'}
spec: {tls: [{hosts: [sample.__DOMAIN__]}]}
'''
        with patch.object(lab, 'run', return_value=manifest):
            docs = lab.documents(self.cfg)
        self.assertNotIn('nfs', docs[0]['spec'])
        self.assertEqual(docs[0]['spec']['hostPath']['type'], 'DirectoryOrCreate')
        affinity = docs[0]['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]['matchExpressions'][0]['values']
        self.assertEqual(affinity, [self.cfg['cluster_name'] + '-worker'])
        self.assertIn('tls', docs[1]['spec'])
        self.assertIn('cert-manager.io/cluster-issuer', docs[1]['metadata']['annotations'])

    def test_rejects_embedded_secret(self):
        with patch.object(lab, 'run', return_value='apiVersion: v1\nkind: Secret\nmetadata: {name: bad}\n'), self.assertRaises(lab.Error):
            lab.documents(self.cfg)

    def test_configuration_pvcs_are_isolated_and_preserve_mount_layout(self):
        pod = {'volumes': [
            {'name': 'shared', 'hostPath': {'path': '__NFS_ROOT__/mediaserver-config', 'type': 'Directory'}},
            {'name': 'device', 'hostPath': {'path': '/dev/dri', 'type': 'Directory'}},
            {'name': 'socket', 'hostPath': {'path': '/var/run/example.sock', 'type': 'Socket'}},
        ], 'containers': [
            {'name': 'one', 'image': 'registry.k8s.io/pause:3.10', 'volumeMounts': [{'name': 'shared', 'mountPath': '/config', 'subPath': 'one', 'readOnly': True}]},
            {'name': 'two', 'image': 'registry.k8s.io/pause:3.10', 'volumeMounts': [{'name': 'shared', 'mountPath': '/config', 'subPath': 'two'}]},
        ]}
        objects = []
        for kind in ('Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'CronJob'):
            template = {'template': {'spec': copy.deepcopy(pod)}}
            spec = {'jobTemplate': {'spec': template}} if kind == 'CronJob' else template
            objects.append({'apiVersion': 'batch/v1' if kind in ('Job', 'CronJob') else 'apps/v1', 'kind': kind, 'metadata': {'name': kind.lower(), 'namespace': 'sample'}, 'spec': spec})
        manifest = yaml.safe_dump_all(objects)
        self.cfg['storage'].update(class_name='local-path', config_size='7Gi')
        for profile, expected_class in [('kind', 'standard'), ('server', 'local-path')]:
            self.cfg['profile'] = profile
            with patch.object(lab, 'run', return_value=manifest):
                docs = lab.documents(self.cfg)
            claims = {d['metadata']['name']: d for d in docs if d['kind'] == 'PersistentVolumeClaim'}
            workloads = [d for d in docs if d['kind'] != 'PersistentVolumeClaim']
            self.assertEqual(len(claims), len(workloads))
            for obj in workloads:
                rendered = lab.pod_spec(obj)
                name = obj['metadata']['name'] + '-shared'
                self.assertEqual(rendered['volumes'][0]['persistentVolumeClaim']['claimName'], name)
                self.assertNotIn('hostPath', rendered['volumes'][0])
                self.assertEqual(rendered['volumes'][1]['hostPath']['path'], '/dev/dri')
                self.assertEqual(rendered['volumes'][2]['hostPath']['type'], 'Socket')
                self.assertEqual(rendered['containers'], pod['containers'])
                self.assertEqual(claims[name]['metadata']['namespace'], 'sample')
                self.assertEqual(claims[name]['spec'], {'accessModes': ['ReadWriteOnce'], 'storageClassName': expected_class, 'resources': {'requests': {'storage': '7Gi'}}})

    def test_nextcloud_kind_data_prep_is_scoped_and_nonrecursive(self):
        pv = {'apiVersion': 'v1', 'kind': 'PersistentVolume', 'metadata': {'name': 'nextcloud-nfs-pv'}, 'spec': {'nfs': {'server': '__NFS_SERVER__', 'path': '__NFS_ROOT__/nextcloud'}}}
        pod = {'securityContext': {'fsGroup': 33}, 'volumes': [
            {'name': 'nextcloud-main', 'persistentVolumeClaim': {'claimName': 'nextcloud-nfs-pvc'}},
            {'name': 'nextcloud-config', 'configMap': {'name': 'nextcloud-config'}},
        ], 'containers': [{'name': 'nextcloud', 'image': 'registry.k8s.io/pause:3.10', 'volumeMounts': [
            {'name': 'nextcloud-main', 'mountPath': '/var/www', 'subPath': 'root'},
            {'name': 'nextcloud-main', 'mountPath': '/var/www/html/config', 'subPath': 'config'},
            {'name': 'nextcloud-config', 'mountPath': '/var/www/html/config/app.php', 'subPath': 'app.php'},
        ]}]}
        deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'nextcloud', 'namespace': 'nextcloud'}, 'spec': {'template': {'spec': pod}}}
        self.cat['apps']['nextcloud'] = {'path': 'applications/sample', 'namespace': 'nextcloud', 'kind_supported': True, 'secrets': {}}
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        self.cfg['applications'] = ['nextcloud']
        manifest = yaml.safe_dump_all([pv, deployment])
        with patch.object(lab, 'run', return_value=manifest):
            docs = lab.documents(self.cfg)
        init = lab.pod_spec(docs[1])['initContainers'][0]
        self.assertEqual(init['name'], 'prepare-nextcloud-data')
        self.assertEqual(init['args'][2:], ['33', '/data', '/data/root', '/data/config'])
        self.assertEqual(init['volumeMounts'], [{'name': 'nextcloud-main', 'mountPath': '/data'}])
        self.assertNotIn('-R', init['args'][0])
        self.assertNotIn('chown', init['args'][0])
        self.assertEqual(init['securityContext']['capabilities']['add'], ['CHOWN', 'FOWNER'])
        self.cfg['profile'] = 'server'
        with patch.object(lab, 'run', return_value=manifest):
            server = lab.documents(self.cfg)
        self.assertNotIn('initContainers', lab.pod_spec(server[1]))

    def test_http_only_ingress_gets_central_managed_tls(self):
        manifest = '''apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sample
  annotations: {traefik.ingress.kubernetes.io/router.entrypoints: web}
spec:
  rules:
    - host: first.__DOMAIN__
    - host: second.__DOMAIN__
'''
        for mode, issuer in [('selfsigned', 'local-selfsigned'), ('acme', 'public-acme')]:
            self.cfg['tls']['mode'] = mode
            with self.subTest(mode=mode), patch.object(lab, 'run', return_value=manifest):
                ingress = lab.documents(self.cfg)[0]
            self.assertEqual(ingress['spec']['tls'], [{'hosts': ['first.' + self.cfg['domain'], 'second.' + self.cfg['domain']], 'secretName': 'sample-tls'}])
            annotations = ingress['metadata']['annotations']
            self.assertEqual(annotations['cert-manager.io/cluster-issuer'], issuer)
            self.assertEqual(annotations['traefik.ingress.kubernetes.io/router.entrypoints'], 'websecure')
            self.assertEqual(annotations['traefik.ingress.kubernetes.io/router.tls'], 'true')

    def test_managed_tls_preserves_existing_named_secret(self):
        manifest = '''apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: sample}
spec:
  rules: [{host: existing.__DOMAIN__}]
  tls: [{hosts: [existing.__DOMAIN__], secretName: operator-selected-tls}]
'''
        with patch.object(lab, 'run', return_value=manifest):
            ingress = lab.documents(self.cfg)[0]
        self.assertEqual(ingress['spec']['tls'][0]['secretName'], 'operator-selected-tls')

    def test_duplicate_tls_entries_merge_hosts_and_only_generate_missing(self):
        manifest = """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: sample}
spec:
  rules: [{host: first.__DOMAIN__}, {host: second.__DOMAIN__}, {host: missing.__DOMAIN__}]
  tls:
    - {hosts: [first.__DOMAIN__], secretName: original-tls}
    - {hosts: [first.__DOMAIN__, second.__DOMAIN__], secretName: original-tls}
"""
        with patch.object(lab, 'run', return_value=manifest):
            ingress = lab.documents(self.cfg)[0]
        self.assertEqual(ingress['spec']['tls'], [
            {'hosts': ['first.' + self.cfg['domain'], 'second.' + self.cfg['domain']], 'secretName': 'original-tls'},
            {'hosts': ['missing.' + self.cfg['domain']], 'secretName': 'sample-tls'},
        ])

    def test_server_storage_class_and_kind_override(self):
        manifest = '''apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: sample}
spec: {storageClassName: microk8s-hostpath}
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: sample}
spec:
  volumeClaimTemplates:
    - metadata: {name: data}
      spec: {storageClassName: hostpath}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: static-nfs}
spec: {storageClassName: nfs}
'''
        self.cfg['storage']['class_name'] = 'local-path'
        for profile, expected in [('server', 'local-path'), ('kind', 'standard')]:
            self.cfg['profile'] = profile
            with patch.object(lab, 'run', return_value=manifest):
                docs = lab.documents(self.cfg)
            self.assertEqual(docs[0]['spec']['storageClassName'], expected)
            self.assertEqual(docs[1]['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'], expected)
            self.assertEqual(docs[2]['spec']['storageClassName'], 'nfs')

    def test_storage_class_default_and_validation(self):
        self.cfg['storage'].pop('class_name', None)
        self.write()
        self.assertEqual(lab.config(self.path)['storage']['class_name'], 'microk8s-hostpath')
        self.cfg['storage']['class_name'] = 'INVALID/name'
        self.write()
        with self.assertRaises(lab.Error):
            lab.config(self.path)

    def test_server_ingress_ip_matches_dns_and_kind_stays_clusterip(self):
        manifest = '''apiVersion: v1
kind: Service
metadata: {name: traefik, namespace: traefik}
spec: {type: LoadBalancer, ports: [{port: 443}]}
---
apiVersion: v1
kind: Service
metadata: {name: other-service, namespace: other}
spec: {type: LoadBalancer, ports: [{port: 8080}]}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: dns, namespace: dns}
data: {zone: 'app.__DOMAIN__ IN A __LOADBALANCER_IP__'}
'''
        self.cfg['profile'] = 'server'
        self.cfg['domain'] = 'lab.example.net'
        self.write()
        cfg = lab.config(self.path)
        with patch.object(lab, 'run', return_value=manifest):
            server = lab.documents(cfg)
        self.assertEqual(server[0]['spec']['loadBalancerIP'], cfg['network']['loadbalancer_ip'])
        self.assertIn(' IN A ' + server[0]['spec']['loadBalancerIP'], server[2]['data']['zone'])
        self.assertNotIn('loadBalancerIP', server[1]['spec'])
        cfg['profile'] = 'kind'
        with patch.object(lab, 'run', return_value=manifest):
            kind = lab.documents(cfg)
        self.assertEqual(kind[0]['spec']['type'], 'ClusterIP')
        self.assertNotIn('loadBalancerIP', kind[0]['spec'])

    def test_dependency_order_and_cycle(self):
        apps = {'frontend': {'depends_on': ['database']}, 'database': {}}
        self.assertEqual(lab.app_order(['frontend'], apps), ['database', 'frontend'])
        apps['database']['depends_on'] = ['frontend']
        with self.assertRaises(lab.Error):
            lab.app_order(['frontend'], apps)

    def test_loadbalancer_pool_excludes_nfs_and_contains_service_ip(self):
        self.cfg['network']['loadbalancer_pool'] = '192.0.2.10-192.0.2.30'
        self.write()
        with self.assertRaises(lab.Error):
            lab.config(self.path)
        self.cfg['network']['loadbalancer_pool'] = '192.0.2.100-192.0.2.120'
        self.write()
        with self.assertRaises(lab.Error):
            lab.config(self.path)

    def test_acme_requires_server_and_email(self):
        self.cfg['tls'] = {'mode': 'acme', 'email': 'admin@example.test'}
        self.write()
        with self.assertRaises(lab.Error):
            lab.config(self.path)

    def test_malformed_secret_yaml_does_not_disclose_contents(self):
        self.cat['apps']['sample']['secrets'] = {'sample-credentials': ['password']}
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        (self.path.parent / 'age.agekey').write_text('test-key')
        (self.path.parent / 'secrets.sops.yaml').write_text('ciphertext')
        with patch.object(lab, 'run', return_value='password: [DO_NOT_DISCLOSE'), self.assertRaises(lab.Error) as result:
            lab.secret_store(self.cfg, self.path)
        self.assertNotIn('DO_NOT_DISCLOSE', str(result.exception))

    def test_sops_error_never_writes_plaintext_diagnostic(self):
        result = argparse.Namespace(returncode=1, stderr="synthetic-sensitive-input", stdout="")
        with patch.object(lab.subprocess, "run", return_value=result):
            with self.assertRaises(lab.Error) as error:
                lab.run(["sops", "encrypt"], data="password: synthetic-sensitive-input")
        self.assertNotIn("synthetic-sensitive-input", str(error.exception))
        self.assertFalse((self.root / "local/diagnostics/last-command-error.log").exists())

    def test_output_and_config_stay_local(self):
        with self.assertRaises(lab.Error):
            lab.local_path(self.root / 'applications/sample/resources.yaml')
        self.assertEqual(lab.local_path(self.path), self.path.resolve())

    @unittest.skipUnless(shutil.which('sops') and shutil.which('age-keygen'), 'sops and age-keygen required')
    def test_encrypted_secrets_preserved_and_never_rendered(self):
        self.cat['apps']['sample']['secrets'] = {'sample-credentials': ['password']}
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        first = lab.secret_store(self.cfg, self.path, create=True)
        encrypted = self.path.parent / 'secrets.sops.yaml'
        before = encrypted.read_bytes()
        second = lab.secret_store(self.cfg, self.path, create=True)
        self.assertEqual(first, second)
        self.assertEqual(before, encrypted.read_bytes())
        self.assertNotIn(first['sample']['sample-credentials']['password'], encrypted.read_text())
        self.assertEqual(encrypted.stat().st_mode & 0o777, 0o600)
        (self.path.parent / 'age.agekey').unlink()
        with self.assertRaises(lab.Error):
            lab.secret_store(self.cfg, self.path, create=True)

    @unittest.skipUnless(shutil.which('sops') and shutil.which('age-keygen'), 'sops and age-keygen required')
    def test_external_provider_inputs_are_not_generated(self):
        external = ['anthropic', 'OPENVPN_PASSWORD', 'OPENVPN_USER', 'VPN_SERVICE_PROVIDER', 'VPN_TYPE', 'smtp-host', 'smtp-password', 'smtp-username', 'UNKNOWN_PROVIDER_INPUT']
        self.cat['apps']['sample']['secrets'] = {'sample-credentials': external + ['AUTHENTIK_BOOTSTRAP_TOKEN']}
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        values = lab.secret_store(self.cfg, self.path, create=True)['sample']['sample-credentials']
        self.assertTrue(all(values[key] == '' for key in external))
        self.assertGreaterEqual(len(values['AUTHENTIK_BOOTSTRAP_TOKEN']), 32)
        self.assertEqual(values, lab.secret_store(self.cfg, self.path, create=True)['sample']['sample-credentials'])

    @unittest.skipUnless(shutil.which('sops') and shutil.which('age-keygen'), 'sops and age-keygen required')
    def test_shared_secret_uses_identical_credentials(self):
        self.cat['apps']['sample']['secrets'] = {'shared-credentials': ['password', 'username']}
        self.cat['apps']['extension'] = {'namespace': 'sample', 'secrets': {'shared-credentials': ['password']}}
        (self.root / 'catalog.yaml').write_text(yaml.safe_dump(self.cat))
        self.cfg['applications'].append('extension')
        first = lab.secret_store(self.cfg, self.path, create=True)
        self.assertEqual(first['sample']['shared-credentials'], first['extension']['shared-credentials'])
        second = lab.secret_store(self.cfg, self.path, create=True)
        self.assertEqual(first, second)

    def test_configure_repeat_preserves_inputs(self):
        args = argparse.Namespace(config=str(self.path), profile=None, non_interactive=True)
        lab.configure(args)
        before = self.path.read_bytes()
        lab.configure(args)
        self.assertEqual(before, self.path.read_bytes())

    def test_server_wizard_accepts_omitted_optional_pool(self):
        self.cfg['profile'] = 'server'
        self.cfg['domain'] = 'lab.example.net'
        self.cfg['network'].pop('loadbalancer_pool')
        self.write()
        args = argparse.Namespace(config=str(self.path), profile=None, non_interactive=False)
        with patch('builtins.input', return_value=''):
            lab.configure(args)
        self.assertEqual(lab.config(self.path)['network']['loadbalancer_pool'], '192.0.2.10-192.0.2.19')


if __name__ == '__main__':
    unittest.main()
