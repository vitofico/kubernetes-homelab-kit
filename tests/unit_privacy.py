"""Prevent instance preferences from returning to the public application bases."""
from pathlib import Path
import re
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class NeutralDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objects = []
        loader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
        for path in (ROOT / 'applications').glob('*/resources.yaml'):
            text = path.read_text()
            if not any(token in text for token in ('name: TZ', 'TZ:', 'MAIL_DOMAIN', 'default_phone_region')):
                continue
            for obj in yaml.load_all(text, Loader=loader):
                if isinstance(obj, dict) and obj.get('kind') != 'CustomResourceDefinition':
                    cls.objects.append((path.parent.name, obj))

    def walk(self, value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self.walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from self.walk(child)

    def test_timezones_are_neutral(self):
        for app, obj in self.objects:
            for value in self.walk(obj):
                if value.get('name') == 'TZ' and 'value' in value:
                    self.assertTrue(value['value'] == 'UTC', f'{app}: non-neutral timezone default')
                if isinstance(value.get('TZ'), str):
                    self.assertTrue(value['TZ'] == 'UTC', f'{app}: non-neutral ConfigMap timezone')

    def test_mail_domain_comes_from_instance_configuration(self):
        for app, obj in self.objects:
            for value in self.walk(obj):
                if value.get('name') == 'MAIL_DOMAIN' and 'value' in value:
                    self.assertTrue(value['value'] == '__DOMAIN__', f'{app}: hardcoded mail-provider domain')

    def test_no_hardcoded_phone_region(self):
        pattern = re.compile(r'''['"]default_phone_region['"]\s*=>\s*['"][^'"]+['"]''')
        for app, obj in self.objects:
            if obj.get('kind') == 'ConfigMap':
                for value in obj.get('data', {}).values():
                    if isinstance(value, str):
                        self.assertFalse(pattern.search(value), f'{app}: hardcoded phone-region preference')
