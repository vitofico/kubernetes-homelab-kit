#!/usr/bin/env python3
"""Small, explicit configuration/render/deploy interface for the template."""
import argparse
import copy
import importlib.util
import ipaddress
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required. Run make tools, then retry.")

ROOT = Path(__file__).resolve().parent.parent
_hermes_spec = importlib.util.spec_from_file_location('homelab_hermes', Path(__file__).with_name('hermes.py'))
hermes = importlib.util.module_from_spec(_hermes_spec)
_hermes_spec.loader.exec_module(hermes)
MARKER = re.compile(r"__[A-Z][A-Z0-9_]*__")
SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
SAFE_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)
INTERNAL_SECRET_KEYS = {
    "password", "db-password", "mariadb-root-password", "redis-password", "postgres-password",
    "DB_PASSWORD", "POSTGRES_PASSWORD", "FTP_PASS", "HERMES_WEBHOOK_SECRET", "webhook-shared-secret",
    "AUTHENTIK_BOOTSTRAP_PASSWORD", "AUTHENTIK_BOOTSTRAP_TOKEN", "AUTHENTIK_POSTGRESQL__PASSWORD", "AUTHENTIK_SECRET_KEY",
    "REPAIR_TOKEN",
}


def load_all(text):
    return yaml.load_all(text, Loader=SAFE_LOADER)


def dump_all(objects, **kwargs):
    return yaml.dump_all(objects, Dumper=SAFE_DUMPER, **kwargs)


class Error(Exception):
    pass


def run(args, *, data=None, env=None):
    """Never include command output in errors: it may contain secret material."""
    try:
        p = subprocess.run([str(a) for a in args], input=data, text=True,
                           capture_output=True, env=env, check=False)
    except FileNotFoundError as exc:
        raise Error(f"Required tool missing: {args[0]}") from exc
    if p.returncode:
        diagnostic = ""
        # Never log server errors from a Secret apply: kubectl can echo input.
        secret_input = data and (Path(str(args[0])).name == "sops" or re.search(r"(?m)^kind: Secret$", data))
        if not secret_input:
            log = ROOT / "local/diagnostics/last-command-error.log"
            write_private(log, p.stderr)
            diagnostic = f"; details: {log}"
        raise Error(f"{Path(str(args[0])).name} failed (exit {p.returncode}){diagnostic}")
    return p.stdout


def dump(obj):
    class NoAliases(SAFE_DUMPER):
        def ignore_aliases(self, data):
            return True
    return yaml.dump(obj, Dumper=NoAliases, sort_keys=False)


def load(path):
    try:
        return yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise Error(f"Cannot read valid YAML: {path}") from exc


def catalog():
    obj = load(ROOT / "catalog.yaml")
    if not isinstance(obj, dict) or not isinstance(obj.get("apps"), dict):
        raise Error("catalog.yaml must contain an apps mapping")
    return obj["apps"]


def app_order(selected, available):
    ordered, active = [], set()
    def visit(app):
        if app in active:
            raise Error("Catalog has a dependency cycle")
        if app in ordered:
            return
        if app not in available:
            raise Error(f"Unknown application: {app}")
        active.add(app)
        for dep in available[app].get("depends_on", []):
            visit(dep)
        active.remove(app)
        ordered.append(app)
    for app in selected:
        visit(app)
    return ordered


def exact_keys(obj, required, where):
    if not isinstance(obj, dict) or set(obj) != set(required):
        raise Error(f"{where} requires exactly: {', '.join(required)}")


def config(path):
    cfg = load(path)
    if isinstance(cfg, dict):
        cfg.setdefault('hermes', copy.deepcopy(hermes.DEFAULTS))
    exact_keys(cfg, ["profile", "cluster_name", "domain", "tls", "applications", "storage", "network", "images", "hermes"], "site.yaml")
    if cfg["profile"] not in ("kind", "server"):
        raise Error("profile must be kind or server")
    if not isinstance(cfg["cluster_name"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,50}", cfg["cluster_name"]):
        raise Error("cluster_name must be a lowercase DNS label")
    domain = cfg["domain"]
    if not isinstance(domain, str) or len(domain) > 253 or "." not in domain or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in domain.split(".")):
        raise Error("domain must be a lowercase DNS name")
    exact_keys(cfg["tls"], ["mode", "email"], "tls")
    if cfg["tls"]["mode"] not in ("selfsigned", "acme") or not isinstance(cfg["tls"]["email"], str):
        raise Error("tls.mode must be selfsigned or acme; tls.email must be a string")
    if cfg["tls"]["mode"] == "acme":
        if cfg["profile"] != "server" or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cfg["tls"]["email"]):
            raise Error("ACME requires server profile and a valid tls.email")
    apps = cfg["applications"]
    available = catalog()
    if not isinstance(apps, list) or not apps or any(not isinstance(x, str) for x in apps) or len(set(apps)) != len(apps):
        raise Error("applications must be a nonempty list without duplicates")
    apps = app_order(apps, available)
    cfg["applications"] = apps
    for app in apps:
        if app not in available:
            raise Error(f"Unknown application: {app}")
        if cfg["profile"] == "kind" and not available[app].get("kind_supported", False):
            raise Error(f"{app} is not supported by the kind profile: {available[app].get('reason', 'requires server integration')}")
    storage = cfg["storage"]
    if not isinstance(storage, dict) or not {"nfs_server", "nfs_root"}.issubset(storage) or set(storage) - {"nfs_server", "nfs_root", "class_name", "config_size"}:
        raise Error("storage requires nfs_server and nfs_root; optional class_name and config_size")
    storage.setdefault("config_size", "10Gi")
    if not isinstance(storage["config_size"], str) or not re.fullmatch(r"[1-9][0-9]*(?:Ki|Mi|Gi|Ti|Pi|Ei)?", storage["config_size"]):
        raise Error("storage.config_size must be a positive integer Kubernetes storage quantity, e.g. 10Gi")
    storage.setdefault("class_name", "microk8s-hostpath")
    if not isinstance(storage["class_name"], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", storage["class_name"]):
        raise Error("storage.class_name must be a lowercase DNS label (1-63 characters)")
    net = cfg["network"]
    if not isinstance(net, dict) or not {"lan_cidr", "loadbalancer_ip"}.issubset(net) or set(net) - {"lan_cidr", "loadbalancer_ip", "loadbalancer_pool", "ftp_ip"}:
        raise Error("network requires lan_cidr and loadbalancer_ip; optional loadbalancer_pool and ftp_ip")
    for section, key in (('storage', 'nfs_server'), ('network', 'lan_cidr'), ('network', 'loadbalancer_ip')):
        if not isinstance(cfg[section][key], str):
            raise Error(f'{section}.{key} must be a string')
    try:
        ipaddress.ip_address(cfg["storage"]["nfs_server"])
        lan = ipaddress.ip_network(cfg["network"]["lan_cidr"], strict=True)
        lb = ipaddress.ip_address(cfg["network"]["loadbalancer_ip"])
        if lb not in lan:
            raise ValueError()
        pool = net.get("loadbalancer_pool", str(lb) + "-" + str(lb + 9))
        if not isinstance(pool, str) or len(pool.split("-")) != 2:
            raise ValueError()
        start, end = [ipaddress.ip_address(x) for x in pool.split("-")]
        nfs = ipaddress.ip_address(cfg["storage"]["nfs_server"])
        if start not in lan or end not in lan or not start <= lb <= end or start > end or (nfs.version == start.version and start <= nfs <= end):
            raise ValueError()
        net["loadbalancer_pool"] = pool
        if 'ftp_ip' in net:
            if not isinstance(net['ftp_ip'], str):
                raise ValueError()
            ftp = ipaddress.ip_address(net['ftp_ip'])
            if ftp not in lan or not start <= ftp <= end or ftp in (lb, nfs):
                raise ValueError()
        elif cfg['profile'] == 'server' and 'ftp-camera' in apps:
            raise Error('ftp-camera requires network.ftp_ip inside the LAN and loadbalancer_pool, distinct from ingress and NFS addresses')
    except (ValueError, TypeError):
        raise Error("Provide valid NFS IP, LAN CIDR and loadbalancer_pool containing loadbalancer_ip, excluding NFS; optional ftp_ip must be inside that pool and distinct from ingress/NFS") from None
    nfsroot = cfg["storage"]["nfs_root"]
    if not isinstance(nfsroot, str) or not nfsroot.startswith("/") or ".." in Path(nfsroot).parts or not re.fullmatch(r"/[A-Za-z0-9_./-]+", nfsroot):
        raise Error("nfs_root must be an absolute path without traversal or whitespace")
    images = cfg["images"]
    if not isinstance(images, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_./:@-]+", v) for k, v in images.items()):
        raise Error("images must map image marker names to container references")
    for app in apps:
        for name in available[app].get("required_images", {}):
            key = name.strip("_")
            if name not in images and key not in images:
                raise Error(f"{app} requires images.{key}; provide a publicly accessible compatible image")
    if cfg["profile"] == "server" and (domain.endswith(".test") or domain.endswith(".example") or domain == "example.com"):
        raise Error("server profile requires your own configured domain")
    try:
        hermes.validate(cfg)
    except ValueError as exc:
        raise Error(str(exc)) from None
    return cfg


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    path.chmod(0o600)


def local_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT / "local").resolve()):
        raise Error("Instance files and rendered output must be written inside this repository's ignored local/ directory")
    return path


def required_secrets(cfg):
    requirements = {app: copy.deepcopy(catalog()[app]['secrets']) for app in cfg['applications'] if catalog()[app].get('secrets')}
    for name, keys in hermes.extra_secrets(cfg).items():
        requirements.setdefault('hermes-agent', {}).setdefault(name, []).extend(keys)
    return requirements


def secrets_env(key):
    env = dict(os.environ)
    env["SOPS_AGE_KEY_FILE"] = str(key)
    return env


def secret_store(cfg, cfg_path, *, create=False):
    """Store only locally encrypted values; transient decrypted content stays in memory."""
    folder = local_path(cfg_path).parent
    key = folder / "age.agekey"
    encrypted = folder / "secrets.sops.yaml"
    requirements = required_secrets(cfg)
    if not requirements:
        return {}
    if not key.exists():
        if not create or encrypted.exists():
            raise Error("Local age key missing; restore it rather than replacing encrypted credentials")
        write_private(key, run(["age-keygen"]))
    if encrypted.exists():
        try:
            data = yaml.load(run(["sops", "decrypt", str(encrypted)], env=secrets_env(key)), Loader=SAFE_LOADER)
        except yaml.YAMLError:
            raise Error("Invalid YAML in local encrypted secret store; secret contents withheld") from None
        if not isinstance(data, dict):
            raise Error("Invalid encrypted secret store")
    elif create:
        data = {}
    else:
        raise Error("Run configure to initialize local encrypted secrets")
    changed = False
    shared = {}
    for app, objects in requirements.items():
        appdata = data.setdefault(app, {})
        for name, keys in objects.items():
            identity = (catalog()[app]["namespace"], name)
            values = shared.setdefault(identity, appdata.setdefault(name, {}))
            appdata[name] = values
            for namekey in keys:
                if namekey not in values:
                    if not create:
                        raise Error(f"Missing secret key for {app}; run configure")
                    # Generate only known internal credentials. Unknown/provider
                    # inputs stay empty, so a new catalog key fails closed.
                    values[namekey] = secrets.token_urlsafe(32) if namekey in INTERNAL_SECRET_KEYS else ""
                    if namekey in ("DB_USERNAME", "POSTGRES_USER", "DB_DATABASE_NAME", "POSTGRES_DB"):
                        values[namekey] = app.removesuffix("-deduper")
                    elif namekey in ("db-username", "postgres-user"):
                        values[namekey] = "nextcloud" if app == "nextcloud" else "quire"
                    elif namekey == "username":
                        values[namekey] = "admin"
                    elif namekey == "FTP_USER":
                        values[namekey] = "camera"
                    if namekey == "POSTGRES_PASSWORD" and values.get("DB_PASSWORD"):
                        values[namekey] = values["DB_PASSWORD"]
                    if namekey == "database-url":
                        values[namekey] = ""  # Filled after its component credentials exist.
                    changed = True
            if "database-url" in values and not values["database-url"]:
                values["database-url"] = "postgresql+asyncpg://opds_sync:" + values["postgres-password"] + "@postgres-opds-sync:5432/opds_sync"
                changed = True
    if create and (changed or not encrypted.exists()):
        recipient = run(["age-keygen", "-y", key]).strip()
        content = run(["sops", "encrypt", "--age", recipient, "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin"], data=dump(data))
        write_private(encrypted, content)
    return data


def configure(args):
    path = local_path(args.config)
    cfg = load(path) if path.exists() else load(ROOT / "config/site.example.yaml")
    if isinstance(cfg.get("storage"), dict):
        cfg["storage"].setdefault("class_name", "microk8s-hostpath")
        cfg["storage"].setdefault("config_size", "10Gi")
    if args.profile:
        cfg["profile"] = args.profile
    if not args.non_interactive:
        print("Enter instance settings; press Enter to keep the displayed default.")
        for key in ("profile", "cluster_name", "domain"):
            value = input(f"{key} [{cfg[key]}]: ").strip()
            if value:
                cfg[key] = value
        value = input(f"applications, comma-separated [{','.join(cfg['applications'])}]: ").strip()
        if value:
            cfg["applications"] = [x.strip() for x in value.split(",")]
        if cfg["profile"] == "server":
            for key in ("mode", "email"):
                value = input(f"tls.{key} [{cfg['tls'][key]}]: ").strip()
                if value:
                    cfg["tls"][key] = value
            for group, names in (("storage", ("nfs_server", "nfs_root", "class_name", "config_size")), ("network", ("lan_cidr", "loadbalancer_ip", "loadbalancer_pool", "ftp_ip"))):
                for key in names:
                    # Optional fields may be absent in a valid hand-written site.
                    # Leave an empty pool unset so validation derives it from the
                    # load-balancer address entered immediately before this prompt.
                    value = input(f"{group}.{key} [{cfg[group].get(key, '')}]: ").strip()
                    if value:
                        cfg[group][key] = value
    # Validate before replacing an existing file.
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "site.yaml"
        candidate.write_text(dump(cfg))
        cfg = config(candidate)
        if cfg['hermes']['repair_targets']:
            documents(cfg)
    write_private(path, dump(cfg))
    data = secret_store(cfg, path, create=True)
    if not args.non_interactive and data:
        # Reuse the same encrypted store; secret prompts never echo their values.
        print("Use ./scripts/lab secrets-edit to supply external provider credentials when needed.")
    print(f"Configured {path}; local credentials are never included in rendered manifests.")


def documents(cfg, *, validate_targets=True):
    replacements = {"__DOMAIN__": cfg["domain"], "__NFS_SERVER__": cfg["storage"]["nfs_server"],
                    "__NFS_ROOT__": cfg["storage"]["nfs_root"], "__LAN_CIDR__": cfg["network"]["lan_cidr"],
                    "__LOADBALANCER_IP__": cfg["network"]["loadbalancer_ip"]}
    replacements["__LOADBALANCER_POOL__"] = cfg["network"].get("loadbalancer_pool", cfg["network"]["loadbalancer_ip"] + "-" + str(ipaddress.ip_address(cfg["network"]["loadbalancer_ip"]) + 9))
    if 'ftp_ip' in cfg['network']:
        replacements['__FTP_IP__'] = str(cfg['network']['ftp_ip'])
    for name, value in cfg["images"].items():
        replacements[name if name.startswith("__") else f"__{name}__"] = value
    result = []
    for app in cfg["applications"]:
        entry = catalog()[app]
        path = (ROOT / entry["path"]).resolve()
        if not path.is_relative_to((ROOT / "applications").resolve()):
            raise Error("Catalog application path escapes applications/")
        raw = run(["kustomize", "build", path])
        for key, value in replacements.items():
            raw = raw.replace(key, value)
        missing = MARKER.findall(raw)
        if missing:
            raise Error(f"Unresolved template markers in {app}: {', '.join(sorted(set(missing)))}")
        docs = [x for x in load_all(raw) if x]
        if any(x.get("kind") == "Secret" for x in docs):
            raise Error(f"{app} embeds Secret objects; use the encrypted local store")
        for obj in docs:
            if obj.get("kind") == "Ingress":
                metadata = obj.setdefault("metadata", {})
                metadata.setdefault("annotations", {}).update({
                    "cert-manager.io/cluster-issuer": "local-selfsigned" if cfg["tls"]["mode"] == "selfsigned" else "public-acme",
                    "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                    "traefik.ingress.kubernetes.io/router.tls": "true",
                })
                spec = obj.setdefault("spec", {})
                hosts = list(dict.fromkeys(rule["host"] for rule in spec.get("rules", []) if rule.get("host")))
                grouped = {}
                default_secret = metadata["name"] + "-tls"
                for tls in spec.get("tls", []):
                    secret = tls.get("secretName") or default_secret
                    entry = grouped.setdefault(secret, {"hosts": [], "secretName": secret})
                    for host in tls.get("hosts", []):
                        if host not in entry["hosts"]:
                            entry["hosts"].append(host)
                covered = {host for entry in grouped.values() for host in entry["hosts"]}
                missing = [host for host in hosts if host not in covered]
                if missing:
                    grouped.setdefault(default_secret, {"hosts": [], "secretName": default_secret})["hosts"].extend(missing)
                if not grouped:
                    raise Error(f"Managed TLS requires explicit ingress hosts: {metadata.get('name')}")
                spec["tls"] = list(grouped.values())
        if app == "cert-manager":
            docs = [x for x in docs if x.get("kind") != "ClusterIssuer"]
            if cfg["tls"]["mode"] == "selfsigned":
                docs.extend([
                    {"apiVersion": "cert-manager.io/v1", "kind": "Issuer", "metadata": {"name": "bootstrap-selfsigned", "namespace": "cert-manager"}, "spec": {"selfSigned": {}}},
                    {"apiVersion": "cert-manager.io/v1", "kind": "Certificate", "metadata": {"name": "homelab-root-ca", "namespace": "cert-manager"}, "spec": {"isCA": True, "commonName": cfg["cluster_name"] + " local root CA", "secretName": "homelab-root-ca", "privateKey": {"algorithm": "ECDSA", "size": 256}, "issuerRef": {"name": "bootstrap-selfsigned", "kind": "Issuer"}}},
                    {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "local-selfsigned"}, "spec": {"ca": {"secretName": "homelab-root-ca"}}}
                ])
            else:
                docs.append({"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer", "metadata": {"name": "public-acme"}, "spec": {"acme": {"email": cfg["tls"]["email"], "server": "https://acme-v02.api.letsencrypt.org/directory", "privateKeySecretRef": {"name": "public-acme-account"}, "solvers": [{"http01": {"ingress": {"ingressClassName": "traefik"}}}]}}})
        dynamic_class = "standard" if cfg["profile"] == "kind" else cfg["storage"].get("class_name", "microk8s-hostpath")
        for obj in docs:
            spec = obj.get("spec", {})
            if (cfg["profile"] == "server" and obj.get("kind") == "Service"
                    and spec.get("type") == "LoadBalancer"
                    and obj.get("metadata", {}).get("name") == "traefik"
                    and obj.get("metadata", {}).get("namespace") == "traefik"):
                spec["loadBalancerIP"] = cfg["network"]["loadbalancer_ip"]
            if obj.get("kind") == "PersistentVolumeClaim" and spec.get("storageClassName") in ("microk8s-hostpath", "hostpath"):
                spec["storageClassName"] = dynamic_class
            if obj.get("kind") == "StatefulSet":
                for claim in spec.get("volumeClaimTemplates", []):
                    if claim.get("spec", {}).get("storageClassName") in ("microk8s-hostpath", "hostpath"):
                        claim["spec"]["storageClassName"] = dynamic_class
        config_claims = []
        for obj in docs:
            config_claims.extend(adapt_data_hostpaths(cfg, obj))
        docs.extend(config_claims)
        if cfg["profile"] == "kind":
            mapped_nfs_pvs = {obj["metadata"]["name"] for obj in docs if obj.get("kind") == "PersistentVolume" and "nfs" in obj.get("spec", {})}
            for obj in docs:
                spec = obj.get("spec", {})
                if obj.get("kind") == "PersistentVolume" and "nfs" in spec:
                    del spec["nfs"]
                    spec.pop("mountOptions", None)
                    spec["hostPath"] = {"path": "/data/kubernetes-homelab-kit/" + obj["metadata"]["name"], "type": "DirectoryOrCreate"}
                    spec["nodeAffinity"] = {"required": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [cfg["cluster_name"] + "-worker"]}]}]}}
                if obj.get("kind") == "Service" and spec.get("type") == "LoadBalancer":
                    spec["type"] = "ClusterIP"
                    spec.pop("loadBalancerIP", None)
                    spec.pop("externalTrafficPolicy", None)
                    spec.pop("allocateLoadBalancerNodePorts", None)
                    for port in spec.get("ports", []):
                        port.pop("nodePort", None)
            if app == "nextcloud" and "nextcloud-nfs-pv" in mapped_nfs_pvs:
                for obj in docs:
                    prepare_nextcloud_data(obj)
        result.extend(docs)
    # Several app modules share namespaces. Merge namespace labels; other duplicate
    # identities remain an error rather than silently overriding resources.
    unique, namespaces = [], {}
    for obj in result:
        if obj.get("kind") == "Namespace":
            name = obj["metadata"]["name"]
            if name in namespaces:
                namespaces[name]["metadata"].setdefault("labels", {}).update(obj["metadata"].get("labels", {}))
                continue
            namespaces[name] = obj
        unique.append(obj)
    result = unique
    try:
        result = hermes.augment(cfg, result, ROOT, validate_targets=validate_targets)
    except ValueError as exc:
        raise Error(str(exc)) from None
    identities = [(x.get("apiVersion"), x.get("kind"), x.get("metadata", {}).get("namespace"), x.get("metadata", {}).get("name")) for x in result]
    if len(set(identities)) != len(identities):
        raise Error("Enabled applications render duplicate Kubernetes resource identities")
    return result


def adapt_data_hostpaths(cfg, obj):
    pod = pod_spec(obj)
    if pod is None:
        return []
    claims = []
    for volume in pod.get("volumes", []):
        host = volume.get("hostPath", {})
        source = host.get("path")
        if not isinstance(source, str) or host.get("type", "") not in ("", "Directory", "DirectoryOrCreate"):
            continue
        path, nfsroot = PurePosixPath(source), PurePosixPath(cfg["storage"]["nfs_root"])
        if ".." in path.parts or not path.is_relative_to(nfsroot):
            continue
        if path.is_relative_to(PurePosixPath("/dev")) or source.endswith(".sock"):
            continue
        metadata = obj["metadata"]
        name = metadata["name"] + "-" + volume["name"]
        if len(name) > 63:
            name = name[:50].rstrip("-") + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
        volume.pop("hostPath")
        volume["persistentVolumeClaim"] = {"claimName": name}
        claims.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": metadata.get("namespace", "default")}, "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "standard" if cfg["profile"] == "kind" else cfg["storage"].get("class_name", "microk8s-hostpath"), "resources": {"requests": {"storage": cfg["storage"].get("config_size", "10Gi")}}}})
    return claims


def pod_spec(obj):
    """Select actual pod specifications, never schemas embedded in CRDs."""
    spec = obj.get("spec", {})
    kind = obj.get("kind")
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job"):
        return spec.get("template", {}).get("spec", {})
    return None


def rollout_timeout(obj):
    """Allow the declared startup window, then a bounded readiness grace period."""
    windows = []
    for container in (pod_spec(obj) or {}).get('containers', []):
        probe = container.get('startupProbe')
        if probe:
            windows.append(probe.get('initialDelaySeconds', 0) + probe.get('failureThreshold', 3)
                           * max(probe.get('periodSeconds', 10), probe.get('timeoutSeconds', 1)))
    return max(300, max(windows, default=0) + 120)


def prepare_nextcloud_data(obj):
    """kind hostPath does not implement fsGroup ownership; prepare only data dirs."""
    if obj.get("kind") != "Deployment" or obj.get("metadata", {}).get("name") != "nextcloud" or obj.get("metadata", {}).get("namespace") != "nextcloud":
        return
    pod = pod_spec(obj)
    data = next((v for v in pod.get("volumes", []) if v.get("name") == "nextcloud-main" and v.get("persistentVolumeClaim", {}).get("claimName") == "nextcloud-nfs-pvc"), None)
    if not data:
        return
    group = pod.get("securityContext", {}).get("fsGroup")
    if not isinstance(group, int) or isinstance(group, bool) or group < 0:
        raise Error("kind Nextcloud data preparation requires a numeric fsGroup")
    paths = ["/data"]
    for container in pod.get("containers", []):
        for mount in container.get("volumeMounts", []):
            if mount.get("name") != data["name"] or not mount.get("subPath"):
                continue
            relative = PurePosixPath(mount["subPath"])
            if relative.is_absolute() or ".." in relative.parts:
                raise Error("Nextcloud data subPath must remain inside its data volume")
            path = str(PurePosixPath("/data") / relative)
            if path not in paths:
                paths.append(path)
    script = 'group="$1"; shift; for path in "$@"; do mkdir -p -- "$path"; chgrp -- "$group" "$path"; chmod g+rwx,g+s -- "$path"; done'
    init = {"name": "prepare-nextcloud-data", "image": "docker.io/library/busybox:1.37", "imagePullPolicy": "IfNotPresent", "command": ["sh", "-ec"], "args": [script, "prepare-nextcloud-data", str(group), *paths], "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "FOWNER"]}}, "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}}, "volumeMounts": [{"name": data["name"], "mountPath": "/data"}]}
    pod["initContainers"] = [init] + [c for c in pod.get("initContainers", []) if c.get("name") != init["name"]]


def render(args):
    cfg = config(args.config)
    docs = documents(cfg)
    out = local_path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "resources.yaml"
    write_private(path, dump_all(docs, sort_keys=False))
    print(f"Rendered {len(docs)} resources to {path} (secrets excluded).")
    if args.command == "plan":
        for obj in docs:
            if obj.get("kind") == "Ingress":
                for rule in obj.get("spec", {}).get("rules", []):
                    print(f"  Planned ingress: https://{rule['host']} (TLS {cfg['tls']['mode']}; readiness not checked)")


def kube(args):
    if not args.kubeconfig or not Path(args.kubeconfig).is_file():
        raise Error("An existing explicit --kubeconfig file is required; global kubeconfig is never used")
    return ["kubectl", "--kubeconfig", str(Path(args.kubeconfig).resolve())]


def wait_certmanager(client, timeout=120):
    """Deployment readiness precedes webhook endpoint/CA propagation on fresh clusters."""
    probe = {"apiVersion": "cert-manager.io/v1", "kind": "Issuer", "metadata": {"name": "homelab-webhook-readiness", "namespace": "cert-manager"}, "spec": {"selfSigned": {}}}
    deadline = time.monotonic() + timeout
    command = client + ["apply", "--server-side", "--field-manager=kubernetes-homelab-kit", "--dry-run=server", "--request-timeout=10s", "-f", "-"]
    while True:
        try:
            result = subprocess.run(command, input=dump(probe), capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return
            diagnostic = result.stderr
            lower = diagnostic.lower()
            permanent = any(message in lower for message in ("forbidden", "unauthorized", "the request is invalid", "denied the request"))
            transient = not permanent and any(message in lower for message in (
                "failed calling webhook", "no endpoints available for service",
                "context deadline exceeded", "i/o timeout", "tls handshake timeout",
            ))
        except subprocess.TimeoutExpired:
            diagnostic = "cert-manager admission readiness probe exceeded its request timeout"
            transient = True
        log = ROOT / "local/diagnostics/last-command-error.log"
        write_private(log, diagnostic)
        remaining = deadline - time.monotonic()
        if not transient:
            raise Error(f"cert-manager admission probe rejected the request; details: {log}")
        if remaining <= 0:
            raise Error(f"cert-manager admission API did not become ready within {timeout}s; details: {log}")
        time.sleep(min(2, remaining))


def deploy(args):
    cfg = config(args.config)
    client = kube(args)
    docs = documents(cfg)
    creds = secret_store(cfg, args.config)
    for app, objects in required_secrets(cfg).items():
        for name, keys in objects.items():
            values = creds[app][name]
            if any(not isinstance(values.get(k), str) or not values[k] for k in keys):
                raise Error(f"{app} requires external credentials; edit local secrets with sops before deploying")
    if cfg["profile"] == "kind":
        context = run(client + ["config", "current-context"]).strip()
        if context != "kind-" + cfg["cluster_name"]:
            raise Error("kind profile kubeconfig context must match kind-<cluster_name>")
    def apply(objects):
        if objects:
            run(client + ["apply", "--server-side", "--field-manager=kubernetes-homelab-kit", "-f", "-"], data=dump_all(objects, sort_keys=False))
    crds = [x for x in docs if x.get("kind") == "CustomResourceDefinition"]
    if crds:
        print(f"Preparing {len(crds)} Kubernetes resource definitions...", flush=True)
    apply(crds)
    for obj in crds:
        run(client + ["wait", "--for=condition=Established", "crd/" + obj["metadata"]["name"], "--timeout=120s"])
    apply([x for x in docs if x.get("kind") == "Namespace"])
    for app, objects in required_secrets(cfg).items():
        for name in objects:
            obj = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": catalog()[app]["namespace"]}, "type": "Opaque", "stringData": creds[app][name]}
            if app == "hermes-agent" and name == "hermes-repair-credentials":
                obj["metadata"]["labels"] = {"app.kubernetes.io/part-of": "kubernetes-homelab-kit", "app.kubernetes.io/component": "hermes-repair"}
            apply([obj])
    native_groups = {"v1", "apps", "batch", "autoscaling", "networking.k8s.io", "policy", "rbac.authorization.k8s.io", "admissionregistration.k8s.io", "storage.k8s.io", "scheduling.k8s.io", "coordination.k8s.io"}
    for app in cfg["applications"]:
        print(f"Deploying {app}...", flush=True)
        part_cfg = copy.deepcopy(cfg)
        part_cfg["applications"] = [app]
        objects = [x for x in documents(part_cfg, validate_targets=False) if x.get("kind") not in ("CustomResourceDefinition", "Namespace")]
        native = [x for x in objects if x["apiVersion"].split("/")[0] in native_groups]
        custom = [x for x in objects if x not in native]
        apply(native)
        for obj in native:
            kind = obj.get("kind")
            if kind in ("Deployment", "StatefulSet", "DaemonSet"):
                meta = obj["metadata"]
                print(f"  Waiting for {kind.lower()}/{meta['name']}...", flush=True)
                run(client + ["rollout", "status", f"{kind.lower()}/{meta['name']}", "-n", meta.get("namespace", "default"), f"--timeout={rollout_timeout(obj)}s"])
        if app == "cert-manager" and custom:
            wait_certmanager(client)
        apply(custom)
    print("Enabled applications deployed and workloads ready.")


def doctor(args):
    cfg = config(args.config)
    tools = ["kustomize", "kubectl"]
    if cfg["profile"] == "kind":
        tools.extend(["docker", "kind"])
    if required_secrets(cfg):
        tools.extend(["sops", "age-keygen"])
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        raise Error("Install required tools: " + ", ".join(missing))
    if cfg["profile"] == "kind":
        info = json.loads(run(["docker", "info", "--format", "{{json .}}"] ))
        if info.get("NCPU", 0) < 2 or info.get("MemTotal", 0) < 4 * 1024 ** 3:
            raise Error("The kind baseline requires Docker to have at least 2 CPUs and 4 GiB RAM; larger app selections need more")
    if required_secrets(cfg):
        values = secret_store(cfg, args.config)
        for app, objects in required_secrets(cfg).items():
            for name, keys in objects.items():
                if any(not values.get(app, {}).get(name, {}).get(key) for key in keys):
                    raise Error(f"External credentials missing for {app}; run ./scripts/lab secrets-edit")
    documents(cfg)
    print("Configuration, required tools, and manifest rendering passed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("configure", "doctor", "render", "plan", "validate", "up", "status", "secrets-edit", "lint", "test-unit"):
        p = sub.add_parser(name)
        p.add_argument("--config", default=str(ROOT / "local/site.yaml"))
        if name == "configure":
            p.add_argument("--non-interactive", action="store_true")
            p.add_argument("--profile", choices=["kind", "server"])
        if name in ("render", "plan"):
            p.add_argument("--output", default=str(ROOT / "local/build"))
        if name in ("up", "status"):
            p.add_argument("--kubeconfig", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            configure(args)
        elif args.command in ("render", "plan"):
            render(args)
        elif args.command == "doctor":
            doctor(args)
        elif args.command == "validate":
            docs = documents(config(args.config))
            print(f"Configuration and rendering passed: {len(docs)} resources.")
        elif args.command == "up":
            deploy(args)
        elif args.command == "status":
            cfg = config(args.config)
            for ns in dict.fromkeys(catalog()[a]["namespace"] for a in cfg["applications"]):
                print(run(kube(args) + ["get", "pods,pvc,svc", "-n", ns]), end="")
        elif args.command == "secrets-edit":
            cfg = config(args.config)
            secret_store(cfg, args.config, create=True)
            folder = Path(args.config).resolve().parent
            if not (folder / "secrets.sops.yaml").exists():
                raise Error("Enabled applications do not require secrets")
            result = subprocess.run(["sops", str(folder / "secrets.sops.yaml")], env=secrets_env(folder / "age.agekey"))
            if result.returncode:
                raise Error("Secret editor failed")
        elif args.command == "test-unit":
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-p", "unit*.py", "-v"], check=True)
        elif args.command == "lint":
            for path in list((ROOT / "applications").rglob("*.yaml")) + list((ROOT / "config").rglob("*.yaml")):
                list(load_all(path.read_text()))
            for path in list((ROOT / "scripts").rglob("*.py")) + list((ROOT / "tools").rglob("*.py")) + list((ROOT / "tests").glob("unit*.py")):
                run([sys.executable, "-m", "py_compile", path])
            shells = [ROOT / "scripts/lab"]
            for path in ROOT.rglob("*.sh"):
                if "local" not in path.parts and ".venv" not in path.parts:
                    run(["bash", "-n", path])
                    shells.append(path)
            run(["shellcheck", "--severity=warning", *shells])
            print("YAML/Python syntax and ShellCheck passed.")
    except EOFError:
        print("ERROR: Interactive configuration needs a terminal; use configure --non-interactive or make configure CONFIGURE_FLAGS=--non-interactive.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Configuration cancelled.", file=sys.stderr)
        return 130
    except (Error, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
