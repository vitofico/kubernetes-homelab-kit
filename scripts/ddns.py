"""Configuration contract for the optional one-shot dynamic DNS updater."""
import copy
import ipaddress
import re

DEFAULTS = {'provider': 'duckdns', 'schedule': '*/15 * * * *', 'suspend': True,
            'domains': '', 'hostname': '', 'group': '', 'ipv4': 'auto', 'ipv6': 'preserve'}
CREDENTIALS = {'duckdns': 'DUCKDNS_TOKEN', 'freedns': 'FREEDNS_TOKEN',
               'dynu': 'DYNU_PASSWORD', 'desec': 'DESEC_TOKEN'}


def settings(cfg):
    value = cfg.get('ddns', {})
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('ddns accepts only provider, schedule, suspend, domains, hostname, group, ipv4, ipv6')
    result = copy.deepcopy(DEFAULTS)
    result.update(value)
    return result


def validate(cfg):
    value = settings(cfg)
    cfg['ddns'] = value
    if not isinstance(value['provider'], str) or value['provider'] not in CREDENTIALS:
        raise ValueError('ddns.provider must be duckdns, freedns, dynu or desec')
    # Accept ordinary five-field numeric cron syntax; reject macros, extra fields,
    # names and injected newlines. Kubernetes also validates actual field ranges.
    if not isinstance(value['schedule'], str) or not re.fullmatch(r'[0-9*/,-]+(?: +[0-9*/,-]+){4}', value['schedule']):
        raise ValueError('ddns.schedule must be a five-field numeric cron expression')
    for field, low, high in zip(value['schedule'].split(), (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        for item in field.split(','):
            if not re.fullmatch(r'(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?', item):
                raise ValueError('ddns.schedule has an invalid field')
            bounds, *steps = item.split('/')
            if len(steps) > 1 or (steps and not 1 <= int(steps[0]) <= high - low + 1):
                raise ValueError('ddns.schedule has an invalid step')
            if bounds != '*':
                limits = [int(number) for number in bounds.split('-')]
                if len(limits) > 2 or any(not low <= number <= high for number in limits) or limits != sorted(limits):
                    raise ValueError('ddns.schedule has an invalid field range')
    if type(value['suspend']) is not bool:
        raise ValueError('ddns.suspend must be a boolean')
    for key in ('domains', 'hostname', 'group', 'ipv4', 'ipv6'):
        if not isinstance(value[key], str):
            raise ValueError('ddns.' + key + ' must be a string')
    for key, version in (('ipv4', 4), ('ipv6', 6)):
        text = value[key]
        if text not in ('auto', 'preserve'):
            try:
                if ipaddress.ip_address(text).version != version:
                    raise ValueError()
            except ValueError:
                raise ValueError('ddns.' + key + ' must be auto, preserve, or an address of that family') from None
    if value['ipv4'] == value['ipv6'] == 'auto':
        raise ValueError('Only one ddns IP family may use auto detection')
    if 'dynamic-dns-updater' not in cfg['applications']:
        return
    provider = value['provider']
    if provider in ('duckdns', 'desec'):
        names = value['domains'].split(',')
        if not all(len(name) <= 253 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                                   for label in name.split('.')) for name in names):
            raise ValueError('ddns.domains must be a comma-separated list of DNS names or DuckDNS labels')
        if provider == 'desec' and any('.' not in name for name in names):
            raise ValueError('deSEC requires full hostnames in ddns.domains')
    if provider == 'dynu':
        if bool(value['hostname']) == bool(value['group']):
            raise ValueError('Dynu requires exactly one of ddns.hostname and ddns.group')
        if not re.fullmatch(r'[A-Za-z0-9.-]+', value['hostname'] or value['group']):
            raise ValueError('Dynu hostname or group must contain only letters, numbers, dots or hyphens')


def extra_secrets(cfg):
    if 'dynamic-dns-updater' not in cfg['applications']:
        return {}
    return {'dynamic-dns-credentials': [CREDENTIALS[settings(cfg)['provider']]]}


def augment(cfg, docs):
    if 'dynamic-dns-updater' not in cfg['applications']:
        return docs
    value = settings(cfg)
    job = next(obj for obj in docs if obj.get('kind') == 'CronJob' and obj['metadata']['name'] == 'dynamic-dns-updater')
    job['spec'].update(schedule=value['schedule'], suspend=value['suspend'])
    container = job['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
    provider = value['provider']
    key = CREDENTIALS[provider]
    env = [{'name': 'DNS_PROVIDER', 'value': provider},
           {'name': key, 'valueFrom': {'secretKeyRef': {'name': 'dynamic-dns-credentials', 'key': key}}}]
    values = {'duckdns': {'DUCKDNS_DOMAINS': value['domains']},
              'freedns': {},
              'dynu': {'DYNU_HOSTNAME': value['hostname'], 'DYNU_GROUP': value['group']},
              'desec': {'DESEC_DOMAINS': value['domains'], 'DESEC_IPV4': value['ipv4'], 'DESEC_IPV6': value['ipv6']}}[provider]
    env.extend({'name': name, 'value': text} for name, text in values.items())
    container['env'] = env
    return docs
