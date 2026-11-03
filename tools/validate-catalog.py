#!/usr/bin/env python3
"""Prepare pinned schemas, then validate every catalog module without cluster/network access."""
import argparse
import concurrent.futures
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location('lab', ROOT / 'scripts/lab.py')
lab = importlib.util.module_from_spec(loader)
loader.loader.exec_module(lab)
VERSION = '0.8.0'
KUBERNETES = '1.37.0'
SCHEMA_COMMIT = 'e0c72a1bc5f409d1685c0e1323460c8a98cd7459'
NATIVE_GROUPS = {'', 'apps', 'batch', 'autoscaling', 'networking.k8s.io', 'policy', 'rbac.authorization.k8s.io', 'admissionregistration.k8s.io', 'apiextensions.k8s.io', 'storage.k8s.io', 'scheduling.k8s.io', 'coordination.k8s.io'}
WORK = ROOT / 'local/schema-validation'
NATIVE = WORK / 'native'
CUSTOM = WORK / 'custom'


def download(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as source:
                return source.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise lab.Error('Pinned upstream artifact/schema is missing: ' + url) from None
            if attempt == 2:
                raise lab.Error('Upstream download failed: ' + type(error).__name__) from None
        except OSError as error:
            if attempt == 2:
                raise lab.Error('Upstream download failed: ' + type(error).__name__) from None
        time.sleep(2 ** attempt)


def kubeconform(prepare):
    cached = ROOT / 'local/tools/kubeconform-v0.8.0'
    tool = str(cached) if cached.exists() else shutil.which('kubeconform')
    if tool and subprocess.check_output([tool, '-v'], text=True).strip() == 'v' + VERSION:
        return tool
    if not prepare:
        raise lab.Error('Kubeconform v0.8.0 required; run make validate-all VALIDATE_FLAGS=--prepare')
    system = platform.system().lower()
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if system not in ('darwin', 'linux') or not arch:
        raise lab.Error('Automatic Kubeconform install supports Linux/macOS amd64/arm64')
    asset = f'kubeconform-{system}-{arch}.tar.gz'
    base = f'https://github.com/yannh/kubeconform/releases/download/v{VERSION}/'
    checksums = download(base + 'CHECKSUMS').decode()
    expected = next((line.split()[0] for line in checksums.splitlines() if line.split()[-1].lstrip('*') == asset), None)
    data = download(base + asset)
    if not expected or hashlib.sha256(data).hexdigest() != expected:
        raise lab.Error('Kubeconform release checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        payload = archive.extractfile('kubeconform').read()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(payload)
    cached.chmod(0o700)
    if subprocess.check_output([cached, '-v'], text=True).strip() != 'v' + VERSION:
        raise lab.Error('Downloaded Kubeconform version mismatch')
    return str(cached)


def gvk(obj):
    api = obj['apiVersion'].split('/')
    return (api[0], api[1], obj['kind']) if len(api) == 2 else ('', api[0], obj['kind'])


def native_filename(group, version, kind):
    return kind.lower() + ('-' + group.split('.')[0] if group else '') + '-' + version + '.json'


def json_schema(value):
    """Convert the OpenAPI-only scalar extensions used by embedded CRD schemas."""
    if isinstance(value, list):
        return [json_schema(x) for x in value]
    if not isinstance(value, dict):
        return value
    out = {k: json_schema(v) for k, v in value.items() if k != 'nullable'}
    if value.get('nullable'):
        # OpenAPI nullable applies in addition to the declared schema.
        out = {'anyOf': [out, {'type': 'null'}]}
    if value.get('x-kubernetes-int-or-string') and not value.get('type') and not value.get('anyOf'):
        out['anyOf'] = [{'type': 'integer'}, {'type': 'string'}]
    return out


def require_local_references(value):
    if isinstance(value, dict):
        reference = value.get('$ref')
        if isinstance(reference, str) and not reference.startswith('#'):
            raise lab.Error('Validation schema contains a nonlocal reference; offline validation is required')
        for child in value.values():
            require_local_references(child)
    elif isinstance(value, list):
        for child in value:
            require_local_references(child)


def validation(tool, manifest):
    command = [tool, '-strict', '-summary', '-output', 'json', '-kubernetes-version', KUBERNETES,
               '-schema-location', str(NATIVE / '{{.ResourceKind}}{{.KindSuffix}}.json'),
               '-schema-location', str(CUSTOM / '{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'),
               str(manifest)]
    result = subprocess.run(command, capture_output=True, text=True)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise lab.Error('Kubeconform returned non-JSON output: ' + result.stderr[:200]) from None
    return result.returncode, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help='Allow fetching pinned tool/native schemas before the strictly offline validation pass')
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    tool = kubeconform(args.prepare)
    cfg = lab.load(ROOT / 'config/site.example.yaml')
    catalog = lab.catalog()
    cfg.update(profile='server', domain='schema.example.net', applications=list(catalog))
    # These references are schema fixtures only; no container is started or claimed compatible.
    cfg['images'] = {marker: 'registry.k8s.io/pause:3.10' for entry in catalog.values() for marker in entry.get('required_images', {})}
    site = WORK / 'site.yaml'
    lab.write_private(site, lab.dump(cfg))
    docs = lab.documents(lab.config(site))
    manifest = WORK / 'catalog.yaml'
    lab.write_private(manifest, lab.dump_all(docs, sort_keys=False))
    crds = [d for d in docs if d['kind'] == 'CustomResourceDefinition']
    schema_keys = set()
    for crd in crds:
        spec = crd['spec']
        for version in spec['versions']:
            if not version.get('served'):
                continue
            raw = version.get('schema', {}).get('openAPIV3Schema')
            if not raw:
                raise lab.Error('Embedded CRD has no validation schema: ' + crd['metadata']['name'])
            schema = json_schema(raw)
            schema['$schema'] = 'http://json-schema.org/draft-07/schema#'
            require_local_references(schema)
            dest = CUSTOM / spec['group'] / (spec['names']['kind'].lower() + '_' + version['name'] + '.json')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(schema))
            schema_keys.add((spec['group'], version['name'], spec['names']['kind']))
    native_keys = {gvk(d) for d in docs if gvk(d)[0] in NATIVE_GROUPS}
    unknown = {gvk(d) for d in docs} - native_keys - schema_keys
    if unknown:
        raise lab.Error('Custom resources have no embedded schema: ' + repr(sorted(unknown)))
    def prepare_schema(key):
        name = native_filename(*key)
        destination = NATIVE / name
        if not destination.exists():
            if not args.prepare:
                raise lab.Error('Missing offline schema ' + name + '; run make validate-all VALIDATE_FLAGS=--prepare')
            url = f'https://raw.githubusercontent.com/yannh/kubernetes-json-schema/{SCHEMA_COMMIT}/v{KUBERNETES}-standalone-strict/{name}'
            if key == ('apiextensions.k8s.io', 'v1', 'CustomResourceDefinition'):
                # Recursive JSONSchemaProps cannot be flattened by the upstream
                # standalone generator. Bundle the pinned recursive definitions
                # into the official non-standalone CRD root instead of skipping it.
                base = f'https://raw.githubusercontent.com/yannh/kubernetes-json-schema/{SCHEMA_COMMIT}/v{KUBERNETES}'
                root = json.loads(download(base + '-local/' + name))
                definitions = json.loads(download(base + '/_definitions.json'))['definitions']
                def local_refs(value):
                    if isinstance(value, dict):
                        if isinstance(value.get('$ref'), str):
                            value['$ref'] = value['$ref'].replace('_definitions.json#', '#')
                        for child in value.values():
                            local_refs(child)
                    elif isinstance(value, list):
                        for child in value:
                            local_refs(child)
                local_refs(root)
                root['definitions'] = definitions
                root['additionalProperties'] = False
                data = json.dumps(root).encode()
            else:
                data = download(url)
                json.loads(data)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        require_local_references(json.loads(destination.read_bytes()))
        return name, hashlib.sha256(destination.read_bytes()).hexdigest()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        hashes = dict(pool.map(prepare_schema, sorted(native_keys)))
    # All locations passed to Kubeconform below are local filesystem paths.
    code, report = validation(tool, manifest)
    report.update(modules=len(catalog), resources_rendered=len(docs), custom_resources=sum(gvk(d)[0] not in NATIVE_GROUPS for d in docs), schema_commit=SCHEMA_COMMIT, kubernetes_version=KUBERNETES, kubeconform_version=VERSION, native_schema_sha256=hashes)
    negative = [
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'invalid-probe'}, 'spec': {'replicas': 'not-an-integer'}},
        {'apiVersion': 'metallb.io/v1beta1', 'kind': 'IPAddressPool', 'metadata': {'name': 'invalid-probe'}, 'spec': {'addresses': [17]}},
        {'apiVersion': 'unknown.example/v1', 'kind': 'UnregisteredSchemaProbe', 'metadata': {'name': 'missing-schema'}},
    ]
    for i, obj in enumerate(negative):
        path = WORK / f'negative-{i}.yaml'
        lab.write_private(path, lab.dump(obj))
        invalid, probe_report = validation(tool, path)
        expected = 'invalid' if i < 2 else 'errors'
        if invalid == 0 or not probe_report.get('summary', {}).get(expected):
            raise lab.Error('Negative validation probe unexpectedly passed: ' + obj['kind'])
    report['negative_probes_rejected'] = len(negative)
    (WORK / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    summary = report.get('summary', {})
    print(json.dumps({k: report[k] for k in ('modules', 'resources_rendered', 'custom_resources', 'kubernetes_version', 'kubeconform_version', 'negative_probes_rejected')}, indent=2))
    print('Validation summary:', json.dumps(summary))
    for item in report.get('resources', []):
        if item.get('status') not in ('VALID', 'SKIPPED'):
            print(item.get('kind'), item.get('name'), item.get('status'), item.get('msg'))
    print('Private detailed report:', WORK / 'report.json')
    return code or bool(summary.get('skipped'))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (lab.Error, OSError, ValueError) as error:
        print('ERROR:', str(error), file=sys.stderr)
        sys.exit(1)
