"""Pure configuration checks and Kubernetes objects for the opt-in Hermes module."""
import copy
import ipaddress
import json
import re


LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
DEFAULTS = {'api_cidrs': [], 'api_ports': [443, 6443], 'repair_targets': [], 'telegram': {'enabled': False, 'allowed_users': []}}
REPAIR_LABELS = {'app.kubernetes.io/part-of': 'kubernetes-homelab-kit',
                 'app.kubernetes.io/component': 'hermes-repair'}
PRIVATE_V4 = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
              '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
              '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
              '224.0.0.0/4', '240.0.0.0/4']
# A non-mapped /80 covers unspecified, loopback, and mapped-address space without
# triggering Kubernetes/Go's conversion of mapped exception addresses to IPv4.
PRIVATE_V6 = ['::/80', '64:ff9b::/96', '64:ff9b:1::/48', '100::/64',
              '2001::/23', '2001:db8::/32', '2002::/16', 'fc00::/7', 'fe80::/10', 'ff00::/8']


def settings(cfg):
    return cfg.get('hermes', DEFAULTS)


def validate(cfg):
    value = cfg.setdefault('hermes', copy.deepcopy(DEFAULTS))
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('hermes supports only api_cidrs, api_ports, repair_targets, and telegram')
    for key, default in DEFAULTS.items():
        value.setdefault(key, copy.deepcopy(default))
    cidrs = value['api_cidrs']
    if not isinstance(cidrs, list) or any(not isinstance(cidr, str) for cidr in cidrs):
        raise ValueError('hermes.api_cidrs must be a list of exact API endpoint /32 or /128 CIDRs')
    try:
        networks = [ipaddress.ip_network(cidr, strict=True) for cidr in cidrs]
    except ValueError:
        raise ValueError('hermes.api_cidrs requires exact API endpoint /32 or /128 CIDRs') from None
    if any(net.prefixlen != net.max_prefixlen for net in networks) or len(set(networks)) != len(networks):
        raise ValueError('hermes.api_cidrs requires distinct exact /32 or /128 CIDRs')
    if any(net.version == 6 and net.network_address.ipv4_mapped is not None for net in networks):
        raise ValueError('Use IPv4 /32 notation for mapped IPv4 API endpoints')
    value['api_cidrs'] = [str(net) for net in networks]
    if 'hermes-agent' in cfg['applications'] and not cidrs:
        raise ValueError('hermes-agent requires explicit hermes.api_cidrs for Kubernetes API endpoints')
    ports = value['api_ports']
    if (not isinstance(ports, list) or not ports
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(ports)) != len(ports)):
        raise ValueError('hermes.api_ports must contain distinct integer TCP ports from 1 to 65535')
    targets = value['repair_targets']
    if not isinstance(targets, list):
        raise ValueError('hermes.repair_targets must be a list')
    seen = set()
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {'namespace', 'deployment'}
                or not isinstance(target['namespace'], str) or not re.fullmatch(LABEL, target['namespace'])
                or target['namespace'] in ('agent', 'kube-system')
                or not isinstance(target['deployment'], str) or len(target['deployment']) > 253
                or not all(re.fullmatch(LABEL, part) for part in target['deployment'].split('.'))):
            raise ValueError('Repair targets require exact namespace/deployment names outside agent and kube-system')
        identity = target['namespace'], target['deployment']
        if identity in seen:
            raise ValueError('hermes.repair_targets must not contain duplicates')
        seen.add(identity)
    telegram = value['telegram']
    if not isinstance(telegram, dict) or set(telegram) - {'enabled', 'allowed_users'}:
        raise ValueError('hermes.telegram supports only enabled and allowed_users')
    for key, default in DEFAULTS['telegram'].items():
        telegram.setdefault(key, copy.deepcopy(default))
    if (not isinstance(telegram['enabled'], bool)
            or not isinstance(telegram['allowed_users'], list)):
        raise ValueError('hermes.telegram requires enabled (boolean) and allowed_users (list)')
    users = telegram['allowed_users']
    if any(isinstance(user, bool) or not isinstance(user, (str, int)) or not re.fullmatch(r'[1-9][0-9]*', str(user)) for user in users):
        raise ValueError('Telegram allowed_users must contain positive numeric user IDs')
    telegram['allowed_users'] = [str(user) for user in users]
    if len(set(telegram['allowed_users'])) != len(users) or (telegram['enabled'] and not users):
        raise ValueError('Enabled Telegram requires a nonempty, distinct allowed_users list')


def extra_secrets(cfg):
    if 'hermes-agent' not in cfg['applications']:
        return {}
    result = {}
    if settings(cfg)['telegram']['enabled']:
        result['hermes-agent-secrets'] = ['TELEGRAM_BOT_TOKEN']
    if settings(cfg)['repair_targets']:
        result['hermes-repair-credentials'] = ['REPAIR_TOKEN']
    return result


def object_(kind, name, spec=None, namespace='agent', api='v1', **fields):
    obj = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name, 'namespace': namespace}}
    if name == 'hermes-repair' or name.startswith('hermes-repair-'):
        obj['metadata']['labels'] = dict(REPAIR_LABELS)
    if spec is not None:
        obj['spec'] = spec
    obj.update(fields)
    return obj


def policy(name, app, **rules):
    return object_('NetworkPolicy', name, {'podSelector': {'matchLabels': {'app': app}},
                                         'policyTypes': [key.capitalize() for key in rules], **rules}, api='networking.k8s.io/v1')


def secret_env(name, secret, key=None):
    return {'name': name, 'valueFrom': {'secretKeyRef': {'name': secret, 'key': key or name}}}


def augment(cfg, docs, root, *, validate_targets=True):
    options = settings(cfg)
    targets = options['repair_targets']
    if validate_targets and targets:
        available = {(obj['metadata'].get('namespace', 'default'), obj['metadata']['name'])
                     for obj in docs if obj.get('kind') == 'Deployment'}
        if any((target['namespace'], target['deployment']) not in available for target in targets):
            raise ValueError('Repair targets must name deployments rendered by the selected applications')
    if 'hermes-agent' not in cfg['applications']:
        return docs
    code = {name: (root / 'integrations/hermes' / name).read_text() for name in ('ops.py', 'repair_server.py')}
    skill = (root / 'integrations/hermes/skills/cluster-ops/SKILL.md').read_text()
    docs.extend([object_('ConfigMap', 'hermes-ops-code', data=code),
                 object_('ConfigMap', 'hermes-ops-skill', data={'SKILL.md': skill})])
    agent = next(obj for obj in docs if obj.get('kind') == 'Deployment' and obj['metadata']['name'] == 'hermes-agent')
    container = agent['spec']['template']['spec']['containers'][0]
    if options['telegram']['enabled']:
        container['env'].extend([secret_env('TELEGRAM_BOT_TOKEN', 'hermes-agent-secrets'),
                                 {'name': 'TELEGRAM_ALLOWED_USERS', 'value': ','.join(options['telegram']['allowed_users'])}])
    api_rule = {'to': [{'ipBlock': {'cidr': cidr}} for cidr in options['api_cidrs']],
                'ports': [{'port': port, 'protocol': 'TCP'} for port in options['api_ports']]}
    egress = [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}}],
               'ports': [{'port': 53, 'protocol': protocol} for protocol in ('UDP', 'TCP')]}, copy.deepcopy(api_rule)]
    for version, cidr, excluded in ((4, '0.0.0.0/0', PRIVATE_V4), (6, '::/0', PRIVATE_V6)):
        exceptions = list(excluded)
        for address in [cfg['network']['lan_cidr']] + options['api_cidrs']:
            if ipaddress.ip_network(address).version == version and address not in exceptions:
                exceptions.append(address)
        egress.append({'to': [{'ipBlock': {'cidr': cidr, 'except': exceptions}}], 'ports': [{'port': 443, 'protocol': 'TCP'}]})
    if targets:
        container['env'].extend([{'name': 'REPAIR_URL', 'value': 'http://hermes-repair.agent.svc.cluster.local:8080'},
                                 secret_env('REPAIR_TOKEN', 'hermes-repair-credentials')])
        egress.append({'to': [{'podSelector': {'matchLabels': {'app': 'hermes-repair'}}}], 'ports': [{'port': 8080, 'protocol': 'TCP'}]})
        security = {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532, 'allowPrivilegeEscalation': False,
                    'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
        docs.extend([
            object_('ServiceAccount', 'hermes-repair'),
            object_('Service', 'hermes-repair', {'selector': {'app': 'hermes-repair'}, 'ports': [{'name': 'http', 'port': 8080, 'targetPort': 8080}]}),
            object_('Deployment', 'hermes-repair', {'replicas': 1, 'selector': {'matchLabels': {'app': 'hermes-repair'}},
                'template': {'metadata': {'labels': {'app': 'hermes-repair'}}, 'spec': {
                    'serviceAccountName': 'hermes-repair', 'securityContext': {'seccompProfile': {'type': 'RuntimeDefault'}},
                    'containers': [{'name': 'broker', 'image': 'python:3.13-alpine', 'command': ['python3', '-B', '/opt/hermes-ops/repair_server.py'],
                        'env': [secret_env('REPAIR_TOKEN', 'hermes-repair-credentials'), {'name': 'REPAIR_TARGETS', 'value': json.dumps(targets, separators=(',', ':'))}],
                        'ports': [{'containerPort': 8080, 'name': 'http'}], 'securityContext': security,
                        'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '96Mi'}},
                        'readinessProbe': {'httpGet': {'path': '/health', 'port': 'http'}},
                        'livenessProbe': {'httpGet': {'path': '/health', 'port': 'http'}, 'initialDelaySeconds': 5},
                        'volumeMounts': [{'name': 'code', 'mountPath': '/opt/hermes-ops', 'readOnly': True}]}],
                    'volumes': [{'name': 'code', 'configMap': {'name': 'hermes-ops-code'}}]}}}, api='apps/v1'),
            policy('hermes-repair-ingress', 'hermes-repair', ingress=[{'from': [{'podSelector': {'matchLabels': {'app': 'hermes-agent'}}}], 'ports': [{'port': 8080, 'protocol': 'TCP'}]}]),
            policy('hermes-repair-egress', 'hermes-repair', egress=[copy.deepcopy(api_rule)]),
        ])
        by_namespace = {}
        for target in targets:
            by_namespace.setdefault(target['namespace'], []).append(target['deployment'])
        for namespace, deployments in sorted(by_namespace.items()):
            docs.extend([
                object_('Role', 'hermes-repair', namespace=namespace, api='rbac.authorization.k8s.io/v1',
                        rules=[{'apiGroups': ['apps'], 'resources': ['deployments'], 'resourceNames': sorted(deployments), 'verbs': ['patch']}]),
                object_('RoleBinding', 'hermes-repair', namespace=namespace, api='rbac.authorization.k8s.io/v1',
                        subjects=[{'kind': 'ServiceAccount', 'name': 'hermes-repair', 'namespace': 'agent'}],
                        roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'hermes-repair'}),
            ])
    docs.append(policy('hermes-agent-egress', 'hermes-agent', egress=egress))
    return docs
