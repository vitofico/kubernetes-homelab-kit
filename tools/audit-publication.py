#!/usr/bin/env python3
"""Offline checks over exactly the files eligible for publication. Never prints values."""
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATTERNS = {
    'private key material': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AGE-SECRET-KEY-[A-Z0-9]{20,}',
    'production ciphertext': r'ENC\[AES256_GCM,',
    'local macOS user path': r'/Users/[A-Za-z][^/\s\"\']*/',
    'hardware identifier': r'(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
}

def main():
    p = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT, capture_output=True, check=True)
    paths = sorted(set(x for x in p.stdout.decode().split('\0') if x))
    findings = []
    for rel in paths:
        f = ROOT / rel
        if f.is_symlink():
            findings.append({'file':rel,'rule':'symlink requires review'}); continue
        if not f.is_file():
            continue
        data = f.read_bytes()
        if b'\0' in data:
            findings.append({'file':rel,'rule':'binary requires review'}); continue
        content = data.decode('utf-8', errors='replace')
        for rule, pattern in PATTERNS.items():
            for match in re.finditer(pattern, content):
                findings.append({'file':rel,'line':content.count('\n',0,match.start())+1,'rule':rule})
        for match in re.finditer(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})', content):
            domain = match.group(1).lower()
            reserved = domain.endswith(('.test', '.invalid')) or any(domain == name or domain.endswith('.' + name) for name in ('example.com', 'example.net', 'example.org'))
            if not reserved and match.group(0) != 'git@github.com':
                findings.append({'file':rel,'line':content.count('\n',0,match.start())+1,'rule':'non-example email requires review'})
        if rel.startswith(('local/', '.venv/')):
            findings.append({'file':rel,'rule':'private local file eligible for publication'})
    print(json.dumps({'files_checked':len(paths),'findings':findings},indent=2))
    return bool(findings)

if __name__ == '__main__':
    sys.exit(main())
