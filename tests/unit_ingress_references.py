"""Every independently selected catalog module must route to available Services."""
import copy
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ingress_reference_lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


@unittest.skipUnless(shutil.which('kustomize'), 'kustomize required')
class IngressReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline = yaml.safe_load((ROOT / 'config/site.example.yaml').read_text())
        baseline.update(profile='server', domain='lab.example.net')
        baseline['network']['ftp_ip'] = '192.0.2.11'
        baseline['hermes']['api_cidrs'] = ['192.0.2.1/32']
        baseline['images'] = {marker: f'registry.example.test/{app}:review'
                              for app, entry in lab.catalog().items() for marker in entry.get('required_images', {})}
        cls.rendered = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'site.yaml'
            for app in lab.catalog():
                cfg = copy.deepcopy(baseline)
                cfg['applications'] = [app]
                path.write_text(yaml.safe_dump(cfg))
                cls.rendered[app] = lab.documents(lab.config(path))

    def test_calibre_alone_has_only_its_own_root_route(self):
        docs = self.rendered['calibre-web']
        ingresses = [obj for obj in docs if obj['kind'] == 'Ingress' and obj['metadata']['namespace'] == 'calibre-web']
        routes = [(route['path'], route['backend']['service']['name'])
                  for ingress in ingresses for rule in ingress['spec']['rules'] for route in rule['http']['paths']]
        self.assertEqual(routes, [('/', 'calibre-web')])
        self.assertFalse(any(obj['kind'] == 'Service' and obj['metadata']['name'] == 'quire-server' for obj in docs))

    def test_quire_owns_four_routes_and_keeps_calibre_dependency_root(self):
        docs = self.rendered['quire-server']
        ingresses = {obj['metadata']['name']: obj for obj in docs
                     if obj['kind'] == 'Ingress' and obj['metadata']['namespace'] == 'calibre-web'}
        self.assertEqual(set(ingresses), {'calibre-web', 'quire-server'})
        quire = ingresses['quire-server']
        rules = quire['spec']['rules']
        self.assertEqual([rule['host'] for rule in rules], ['ebooks.lab.example.net'])
        routes = rules[0]['http']['paths']
        self.assertEqual({route['path'] for route in routes}, {'/sync/', '/ai/', '/library/', '/quire-admin'})
        self.assertTrue(all(route['pathType'] == 'Prefix' and route['backend']['service']['name'] == 'quire-server' for route in routes))
        self.assertEqual(ingresses['calibre-web']['spec']['rules'][0]['http']['paths'][0]['path'], '/')
        self.assertEqual(quire['spec']['tls'][0]['secretName'], 'quire-server-tls')
        self.assertEqual(ingresses['calibre-web']['spec']['tls'][0]['secretName'], 'calibre-web-tls')

    def test_each_catalog_selection_resolves_all_ingress_services_and_ports(self):
        checked = 0
        self.assertEqual(len(self.rendered), len(lab.catalog()))
        for app, docs in self.rendered.items():
            services = {(obj['metadata'].get('namespace', 'default'), obj['metadata']['name']): obj
                        for obj in docs if obj['kind'] == 'Service'}
            identities = [(obj['apiVersion'], obj['kind'], obj['metadata'].get('namespace'), obj['metadata']['name']) for obj in docs]
            with self.subTest(application=app):
                self.assertEqual(len(identities), len(set(identities)))
            for ingress in (obj for obj in docs if obj['kind'] == 'Ingress'):
                namespace = ingress['metadata'].get('namespace', 'default')
                backends = [route['backend'] for rule in ingress['spec'].get('rules', []) for route in rule.get('http', {}).get('paths', [])]
                if ingress['spec'].get('defaultBackend'):
                    backends.append(ingress['spec']['defaultBackend'])
                for backend in backends:
                    with self.subTest(application=app, ingress=ingress['metadata']['name'], backend=backend):
                        self.assertIn('service', backend, 'Catalog Ingress needs a declared Service backend')
                        reference = backend['service']
                        key = namespace, reference['name']
                        self.assertIn(key, services, 'Ingress points to a Service outside its selected module/dependencies')
                        port = reference['port']
                        self.assertTrue(any(candidate.get('name') == port['name'] if 'name' in port else candidate['port'] == port['number']
                                            for candidate in services[key]['spec']['ports']), 'Ingress backend port is absent from its Service')
                        checked += 1
        self.assertGreater(checked, 0, 'Expected real rendered Ingress backends')


if __name__ == '__main__':
    unittest.main()
