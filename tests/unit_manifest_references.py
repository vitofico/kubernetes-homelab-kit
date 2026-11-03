"""Catch stale ConfigMap file mounts after template extraction or edits."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ConfigMapReferenceTests(unittest.TestCase):
    def test_workload_configmap_file_mounts_resolve(self):
        objects = []
        loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
        for path in (ROOT / 'applications').glob('*/resources.yaml'):
            objects.extend(obj for obj in yaml.load_all(path.read_text(), Loader=loader)
                           if isinstance(obj, dict) and obj.get('kind') != 'CustomResourceDefinition')
        maps = {(obj['metadata'].get('namespace', 'default'), obj['metadata']['name']): obj
                for obj in objects if obj.get('kind') == 'ConfigMap'}
        checked = 0
        for obj in objects:
            kind, spec = obj.get('kind'), obj.get('spec', {})
            if kind == 'CronJob':
                pod = spec.get('jobTemplate', {}).get('spec', {}).get('template', {}).get('spec', {})
            elif kind in ('Deployment', 'StatefulSet', 'DaemonSet', 'Job', 'ReplicaSet'):
                pod = spec.get('template', {}).get('spec', {})
            elif kind == 'Pod':
                pod = spec
            else:
                continue
            namespace = obj['metadata'].get('namespace', 'default')
            volumes = {volume['name']: volume['configMap'] for volume in pod.get('volumes', []) if 'configMap' in volume}
            for container in pod.get('containers', []) + pod.get('initContainers', []):
                for mount in container.get('volumeMounts', []):
                    if mount.get('name') not in volumes or not mount.get('subPath'):
                        continue
                    reference = volumes[mount['name']]
                    key = (namespace, reference['name'])
                    with self.subTest(workload=obj['metadata']['name'], volume=mount['name']):
                        self.assertIn(key, maps, 'ConfigMap file mount has no declared ConfigMap')
                        configmap = maps[key]
                        paths = [item.get('path', item['key']) for item in reference['items']] if reference.get('items') else list(configmap.get('data', {})) + list(configmap.get('binaryData', {}))
                        subpath = mount['subPath']
                        self.assertTrue(subpath in paths or any(path.startswith(subpath.rstrip('/') + '/') for path in paths), 'ConfigMap subPath has no projected file or directory')
                        checked += 1
        self.assertGreater(checked, 0, 'Expected real application ConfigMap file mounts')
