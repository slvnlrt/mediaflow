"""Installation input and safe first-boot files. No shell evaluation of configuration."""
from __future__ import annotations

import base64
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import CONTAINER_PORTS, Context, ProvisionError, write_private

DEFAULT_PORTS = {**CONTAINER_PORTS, "byparr": 8192}


def _answer(prompt: str, default: str = "") -> str:
    return input(f"{prompt}" + (f" [{default}]" if default else "") + " : ").strip() or default


def _yes(prompt: str, default: bool = False) -> bool:
    return _answer(prompt + " (o/n)", "o" if default else "n").lower() in {"o", "oui", "y", "yes"}


def interactive() -> dict:
    print("MediaFlow — installation Linux, accès LAN. Aucun service n'est exposé sur Internet automatiquement.")
    uid = int(os.environ.get("SUDO_UID", os.getuid()))
    gid = int(os.environ.get("SUDO_GID", os.getgid()))
    if uid == 0:
        uid = int(_answer("UID du compte Linux propriétaire des médias (pas root)", "1000"))
        gid = int(_answer("GID de ce compte", str(uid)))
    # sudo may set HOME=/root; don't strand a non-root media owner under /root.
    default_data = Path.home() / "mediaflow-data"
    if os.getuid() == 0:
        import pwd
        try:
            default_data = Path(pwd.getpwuid(uid).pw_dir) / "mediaflow-data"
        except KeyError:
            default_data = Path("/srv/mediaflow-data")
    data_path = _answer("Dossier des données (sur un seul système de fichiers)",
                        str(default_data))
    bind_ip = _answer("Adresse IPv4 LAN du serveur (127.0.0.1 = accès local seulement)", "127.0.0.1")
    username = _answer("Nom du compte administrateur des applications", "admin")
    email = _answer("Email administrateur Seerr (aucun email envoyé par l'installation)")
    password = getpass.getpass("Mot de passe administrateur (12 caractères minimum) : ")
    if password != getpass.getpass("Confirmer le mot de passe : "):
        raise ProvisionError("Les mots de passe ne correspondent pas")
    timezone = _answer("Fuseau horaire", "Europe/Paris")
    mode = _answer("VPN : none (aucun), proton, custom (autre fournisseur Gluetun)", "none").lower()
    vpn = {"mode": mode, "port_forwarding": False}
    if mode == "none":
        print("ATTENTION : sans VPN, les connexions sortantes utilisent votre IP publique.")
        if not _yes("Continuer explicitement sans VPN"):
            raise ProvisionError("Installation annulée")
    elif mode in {"proton", "custom"}:
        vpn["provider"] = "protonvpn" if mode == "proton" else _answer("Identifiant fournisseur Gluetun")
        vpn["type"] = _answer("Protocole : wireguard ou openvpn", "wireguard")
        if vpn["type"] == "wireguard":
            vpn["wireguard_private_key"] = getpass.getpass("Clé privée WireGuard : ")
            vpn["wireguard_addresses"] = _answer("Adresse(s) WireGuard de la configuration fournisseur")
            if mode == "custom":
                vpn["wireguard_public_key"] = _answer("Clé publique serveur (si requise)")
                vpn["wireguard_endpoint_ip"] = _answer("IP du serveur (si requise)")
                vpn["wireguard_endpoint_port"] = _answer("Port du serveur (si requis)")
        else:
            vpn["openvpn_user"] = _answer("Identifiant OpenVPN dédié (pas le compte web)")
            vpn["openvpn_password"] = getpass.getpass("Mot de passe OpenVPN dédié : ")
        vpn["server_countries"] = _answer("Pays des serveurs VPN (nom Gluetun, vide = automatique)")
        vpn["port_forwarding"] = _yes("Port forwarding compatible avec votre abonnement/configuration")
        if vpn["port_forwarding"] and mode == "proton":
            print("Proton WireGuard exige NAT-PMP activé lors de la génération ; OpenVPN exige +pmp.")
    options = {name: _yes(label) for name, label in [
        ("qui", "Activer QUI et son proxy/cross-seed"),
        ("byparr", "Activer Byparr (seulement pour les indexeurs qui en ont besoin)"),
        ("wud", "Activer WUD (veille uniquement, pas de mises à jour automatiques)"),
    ]}
    return {"schema": 1, "project_name": "mediaflow", "uid": uid, "gid": gid,
            "data_path": data_path, "bind_ip": bind_ip, "timezone": timezone,
            "admin": {"username": username, "password": password, "email": email},
            "vpn": vpn, "options": options, "ports": DEFAULT_PORTS.copy()}


def validate(cfg: dict, root: Path) -> None:
    if cfg.get("schema") != 1:
        raise ProvisionError("Unsupported installation configuration schema")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", cfg.get("project_name", "")):
        raise ProvisionError("Invalid Compose project name")
    for key in ["uid", "gid"]:
        if type(cfg.get(key)) is not int or cfg[key] <= 0:
            raise ProvisionError(f"{key} must be a positive Linux ID, not root")
    try:
        ip = ipaddress.IPv4Address(cfg["bind_ip"])
    except (KeyError, ValueError):
        raise ProvisionError("bind_ip must be an explicit IPv4 address") from None
    if ip.is_multicast or ip.is_unspecified or not (ip.is_private or ip.is_loopback):
        raise ProvisionError("Use a LAN IPv4 address or loopback; public or wildcard publication is not supported")
    raw_path = Path(cfg.get("data_path", ""))
    if not raw_path.is_absolute() or ".." in raw_path.parts:
        raise ProvisionError("data_path must be an absolute path without '..'")
    path = raw_path.resolve()
    forbidden = {Path(x) for x in ["/", "/home", "/root", "/etc", "/usr", "/var", "/opt", "/mnt", "/media", "/srv"]}
    if path in forbidden or path == root or root.is_relative_to(path) or path.is_relative_to(root / ".state"):
        raise ProvisionError("Choose a dedicated media directory, not a system or project parent directory")
    for ancestor in [raw_path, *raw_path.parents]:
        if ancestor.is_symlink():
            raise ProvisionError("Symbolic links are not allowed in the data directory path")
    cfg["data_path"] = str(path)
    admin = cfg.get("admin", {})
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", admin.get("username", "")):
        raise ProvisionError("Admin username must contain only letters, numbers, dots, underscores or hyphens")
    if not isinstance(admin.get("password"), str) or len(admin["password"]) < 12 or "\n" in admin["password"]:
        raise ProvisionError("An admin password of at least 12 characters is required")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", admin.get("email", "")):
        raise ProvisionError("A valid administrator email is required for Seerr")
    try:
        ZoneInfo(cfg.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError):
        raise ProvisionError("Unknown timezone; install the Linux tzdata package") from None
    if set(cfg.get("options", {})) != {"qui", "byparr", "wud"} or any(type(v) is not bool for v in cfg["options"].values()):
        raise ProvisionError("options must define qui, byparr and wud as booleans")
    ports = cfg.get("ports", {})
    if set(ports) != set(DEFAULT_PORTS) or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports.values()):
        raise ProvisionError("Every service needs a host port between 1024 and 65535")
    if len(set(ports.values())) != len(ports):
        raise ProvisionError("Service host ports must be distinct")
    vpn = cfg.get("vpn", {})
    if vpn.get("mode") not in {"none", "proton", "custom"}:
        raise ProvisionError("VPN mode must be none, proton or custom")
    if vpn["mode"] != "none":
        if ports["qbittorrent"] in {8000, 8191, 9999, *(CONTAINER_PORTS[s] for s in ("prowlarr", "radarr", "sonarr"))}:
            raise ProvisionError("qBittorrent WebUI port conflicts with another service in the VPN namespace")
        if not re.fullmatch(r"[a-z0-9][a-z0-9 -]*", vpn.get("provider", "")):
            raise ProvisionError("Invalid Gluetun VPN provider")
        if vpn["mode"] == "proton" and vpn["provider"] != "protonvpn":
            raise ProvisionError("Proton mode must use protonvpn")
        if vpn.get("type") not in {"wireguard", "openvpn"}:
            raise ProvisionError("VPN type must be wireguard or openvpn")
        if vpn["type"] == "wireguard":
            try:
                key = base64.b64decode(vpn.get("wireguard_private_key", ""), validate=True)
                if len(key) != 32:
                    raise ValueError
                for address in vpn.get("wireguard_addresses", "").split(","):
                    ipaddress.ip_interface(address.strip())
            except (ValueError, TypeError):
                raise ProvisionError("Invalid WireGuard private key or interface address") from None
        elif not vpn.get("openvpn_user") or not vpn.get("openvpn_password"):
            raise ProvisionError("Dedicated OpenVPN credentials are required")
        if vpn.get("port_forwarding") and vpn["provider"] not in {"protonvpn", "private internet access", "perfect privacy", "privatevpn"}:
            raise ProvisionError("Native port forwarding is not supported by this provider; disable it")
    cfg.setdefault("managed", {})
    cfg.setdefault("secrets", {})


def initialize_secrets(cfg: dict) -> None:
    for app in ["radarr", "sonarr", "prowlarr"]:
        cfg["secrets"].setdefault(app, secrets.token_hex(16))
    for app in ["seerr", "qui_session", "wud_session"]:
        cfg["secrets"].setdefault(app, secrets.token_hex(32))
    cfg["secrets"].setdefault("wud_password", cfg["admin"]["password"])


def preflight(ctx: Context, *, fresh: bool) -> None:
    if os.name != "posix" or not Path("/proc").is_dir():
        raise ProvisionError("MediaFlow requires Linux with Python 3.11+ and Docker Compose")
    if shutil.which("docker") is None:
        raise ProvisionError("Docker is missing. Install Docker Engine and its Compose plugin first.")
    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint:
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise ProvisionError("Cannot determine the Docker endpoint")
        endpoint = result.stdout.strip()
    if not endpoint.startswith("unix://"):
        raise ProvisionError("Use a local Linux Docker socket; remote Docker contexts are not supported")
    for command in [["docker", "info", "--format", "{{.OSType}}"], ["docker", "compose", "version", "--short"],
                    ["docker", "buildx", "version"]]:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ProvisionError(f"Prerequisite failed: docker {command[1]}. Run bash setup.sh --check.")
        if command[1] == "info" and result.stdout.strip() != "linux":
            raise ProvisionError("A Linux Docker Engine is required")
    if ctx.cfg["vpn"]["mode"] != "none" and not Path("/dev/net/tun").exists():
        raise ProvisionError("VPN selected but /dev/net/tun is unavailable")
    if fresh:
        existing = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={ctx.cfg['project_name']}",
             "--format", "{{.ID}}"], capture_output=True, text=True, timeout=30,
        )
        if existing.returncode or existing.stdout.strip():
            raise ProvisionError("The Docker project already exists or cannot be inspected; choose another project name")
        for service, port in ctx.cfg["ports"].items():
            if service in ctx.cfg["options"] and not ctx.cfg["options"][service]:
                continue
            for host in {"127.0.0.1", ctx.cfg["bind_ip"]}:
                try:
                    with socket.socket() as sock:
                        sock.bind((host, port))
                except OSError:
                    raise ProvisionError(f"Cannot bind {service} port {port} on the selected address") from None
    parent = Path(ctx.cfg["data_path"])
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < 5 * 1024**3:
        raise ProvisionError("At least 5 GiB of free space is required before installation")


def _owned_directory(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        raise ProvisionError("Refusing a symbolic-link runtime directory")
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if created and os.getuid() == 0:
        os.chown(path, uid, gid)
    if not path.is_dir():
        raise ProvisionError("A required directory is occupied by a file")
    info = path.stat()
    required = 0o300 if info.st_uid == uid else 0o030 if info.st_gid == gid else 0o003
    if uid != 0 and info.st_mode & required != required:
        raise ProvisionError(f"The selected media owner cannot write directory: {path}")


def prepare_storage(ctx: Context) -> None:
    """Create only managed media directories and verify cross-directory hardlinks."""
    uid, gid = ctx.cfg["uid"], ctx.cfg["gid"]
    media = Path(ctx.cfg["data_path"])
    _owned_directory(media, uid, gid)
    for relative in ["torrents", "media", "torrents/movies", "torrents/tv", "torrents/incomplete", "torrents/cross-seeds", "media/movies", "media/tv"]:
        _owned_directory(media / relative, uid, gid)
    for source, target in [("torrents/movies", "media/movies"), ("torrents/tv", "media/tv")]:
        fd, name = tempfile.mkstemp(prefix=".mediaflow-hardlink-", dir=media / source)
        os.close(fd)
        link = media / target / Path(name).name
        try:
            os.link(name, link)
            if os.stat(name).st_ino != link.stat().st_ino:
                raise ProvisionError("Storage does not preserve hardlinks")
        except OSError:
            raise ProvisionError("Downloads and library must support hardlinks on the same filesystem") from None
        finally:
            Path(name).unlink(missing_ok=True)
            link.unlink(missing_ok=True)


def prepare(ctx: Context) -> None:
    uid, gid = ctx.cfg["uid"], ctx.cfg["gid"]
    if os.getuid() not in {0, uid}:
        raise ProvisionError("Run as the configured media owner, or root with explicit non-root uid/gid")
    ctx.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    ctx.state.chmod(0o700)
    prepare_storage(ctx)
    for app in [*CONTAINER_PORTS, "gluetun", "recyclarr", "port-sync"]:
        app_uid = uid if app != "seerr" else 1000
        _owned_directory(ctx.state / "apps" / app, app_uid, gid if app != "seerr" else 1000)
    _owned_directory(ctx.state / "apps/jellyfin-cache", uid, gid)
    _owned_directory(ctx.state / "apps/gluetun-status", 0, 0)
    for app in ["radarr", "sonarr", "prowlarr"]:
        path = ctx.state / "apps" / app / "config.xml"
        if not path.exists():
            doc = ET.Element("Config")
            for k, v in {"BindAddress": "*", "Port": str(CONTAINER_PORTS[app]),
                         "ApiKey": ctx.cfg["secrets"][app], "AuthenticationMethod": "Forms",
                         "AuthenticationRequired": "Enabled", "LaunchBrowser": "False",
                         "AnalyticsEnabled": "False", "UpdateMechanism": "Docker"}.items():
                ET.SubElement(doc, k).text = v
            write_private(path, ET.tostring(doc, encoding="unicode"))
            if os.getuid() == 0:
                os.chown(path, uid, gid)
    qbit = ctx.state / "apps/qbittorrent/qBittorrent/qBittorrent.conf"
    _owned_directory(qbit.parent, uid, gid)
    if not qbit.exists():
        salt = secrets.token_bytes(16)
        key = hashlib.pbkdf2_hmac("sha512", ctx.cfg["admin"]["password"].encode(), salt, 100000)
        password_hash = base64.b64encode(salt).decode() + ":" + base64.b64encode(key).decode()
        content = ("[LegalNotice]\nAccepted=true\n\n[WebUI]\n"
                   f"Username={ctx.cfg['admin']['username']}\n"
                   f"Password_PBKDF2=\"@ByteArray({password_hash})\"\n"
                   f"Port={ctx.cfg['ports']['qbittorrent']}\nAddress=*\nLocalHostAuth=true\n"
                   "AuthSubnetWhitelistEnabled=false\nCSRFProtection=true\n"
                   "HostHeaderValidation=true\nServerDomains=*\n"
                   "UseUPnP=false\n\n[BitTorrent]\nSession\\DefaultSavePath=/data/torrents\n"
                   "Session\\TempPath=/data/torrents/incomplete\nSession\\TempPathEnabled=true\n")
        write_private(qbit, content)
        if os.getuid() == 0:
            os.chown(qbit, uid, gid)
    ctx.save()
