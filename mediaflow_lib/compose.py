"""Render a private, fully resolved Compose document for the selected topology."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .common import CONTAINER_PORTS, Context, ProvisionError, write_private

SOURCE_ROOT = Path(__file__).resolve().parent.parent
# These names must remain valid in existing Seerr, Prowlarr and Recyclarr settings.
ARR_VPN_ALIASES = ("radarr", "sonarr")
VPN_WEB_SERVICES = ("qbittorrent", "prowlarr", *ARR_VPN_ALIASES)


def images() -> dict[str, str]:
    return json.loads((SOURCE_ROOT / "images.json").read_text())["images"]


def ensure_network(ctx: Context) -> None:
    name = f"{ctx.cfg['project_name']}-media"
    result = subprocess.run(["docker", "network", "inspect", name], capture_output=True, text=True)
    if result.returncode:
        result = subprocess.run(["docker", "network", "create", "--label",
                                 f"mediaflow.project={ctx.cfg['project_name']}", name],
                                capture_output=True, text=True)
        if result.returncode:
            raise ProvisionError("Cannot create the private application network")
        result = subprocess.run(["docker", "network", "inspect", name], capture_output=True, text=True)
    network = json.loads(result.stdout)[0]
    if network.get("Labels", {}).get("mediaflow.project") != ctx.cfg["project_name"]:
        raise ProvisionError("An unrelated Docker network already uses the selected project name")
    subnets = [entry["Subnet"] for entry in network["IPAM"]["Config"] if ":" not in entry["Subnet"]]
    if len(subnets) != 1:
        raise ProvisionError("Expected one IPv4 application subnet")
    ctx.cfg["network_subnet"] = subnets[0]
    ctx.save()


def _literal(value):
    # Compose performs variable interpolation even on JSON string values.
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [_literal(v) for v in value]
    if isinstance(value, dict):
        return {k: _literal(v) for k, v in value.items()}
    return value


def render(ctx: Context, *, bootstrap: bool = False) -> dict:
    cfg, state = ctx.cfg, ctx.state
    manifest = images()
    # Runtime image overrides are installation-private and used for rollback/pinned upgrades.
    manifest.update(cfg.get("image_overrides", {}))
    published = cfg.get("managed", {}).get("published_services", [])

    def host_for(name: str) -> str:
        return "127.0.0.1" if bootstrap and name not in published else cfg["bind_ip"]

    vpn = cfg["vpn"]["mode"] != "none"
    enabled = ["qbittorrent", "radarr", "sonarr", "prowlarr", "jellyfin", "seerr", "recyclarr"]
    enabled += [name for name, value in cfg["options"].items() if value]
    services = {}
    data = cfg["data_path"]
    identity = {"PUID": str(cfg["uid"]), "PGID": str(cfg["gid"]), "TZ": cfg["timezone"]}
    for name in enabled:
        app = state / "apps" / name
        service = {
            "image": manifest[name], "restart": "unless-stopped", "networks": ["media"],
            "labels": {"mediaflow.managed": "true", "wud.watch": "true"},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }
        if name in {"qbittorrent", "radarr", "sonarr", "prowlarr"}:
            service["environment"] = dict(identity)
            service["volumes"] = [f"{app}:/config"]
            if name != "prowlarr":
                service["volumes"].append(f"{data}:/data")
        elif name == "jellyfin":
            service.update(user=f"{cfg['uid']}:{cfg['gid']}", environment={"TZ": cfg["timezone"]},
                           volumes=[f"{app}:/config", f"{state / 'apps/jellyfin-cache'}:/cache", f"{data}/media:/data/media:ro"])
        elif name == "seerr":
            service.update(init=True, environment={"TZ": cfg["timezone"]}, volumes=[f"{app}:/app/config"])
        elif name == "recyclarr":
            service.update(user=f"{cfg['uid']}:{cfg['gid']}",
                           environment={"TZ": cfg["timezone"], "CRON_SCHEDULE": "0 4 * * *",
                                        "RADARR_API_KEY": cfg["secrets"]["radarr"], "SONARR_API_KEY": cfg["secrets"]["sonarr"]},
                           volumes=[f"{app}:/config"])
        elif name == "qui":
            service.update(user=f"{cfg['uid']}:{cfg['gid']}", environment={"TZ": cfg["timezone"]},
                           volumes=[f"{app}:/config", f"{data}:/data"])
        elif name == "byparr":
            service["environment"] = {"TZ": cfg["timezone"]}
            service["healthcheck"] = {
                "test": ["CMD", "curl", "--fail", "--silent", "http://127.0.0.1:8191/health"],
                "start_period": "30s", "interval": "15s", "timeout": "10s", "retries": 6,
            }
        elif name == "wud":
            service.update(environment={"TZ": cfg["timezone"],
                                        "WUD_AUTH_ADMIN_USER": cfg["admin"]["username"],
                                        "WUD_AUTH_ADMIN_PASSWORD": cfg["secrets"]["wud_password"],
                                        "WUD_AUTH_SESSION_SECRET": cfg["secrets"]["wud_session"],
                                        "WUD_WATCHER_LOCAL_HOST": "socket-proxy",
                                        "WUD_WATCHER_LOCAL_PORT": "2375",
                                        "WUD_WATCHER_LOCAL_WATCHBYDEFAULT": "false",
                                        "WUD_WATCHER_LOCAL_CRON": "0 9 * * *"},
                           volumes=[f"{app}:/store"], networks=["media", "monitor"])
        if name == "qbittorrent":
            service["environment"]["WEBUI_PORT"] = str(ctx.container_port(name))
        if name != "recyclarr" and name != "byparr":
            service["ports"] = [f"{host_for(name)}:{cfg['ports'][name]}:{ctx.container_port(name)}"]
        if vpn and (name in VPN_WEB_SERVICES or name == "byparr"):
            service.pop("networks", None)
            service.pop("ports", None)
            service["network_mode"] = "service:gluetun"
            service["depends_on"] = {"gluetun": {"condition": "service_healthy", "restart": True}}
        services[name] = service
    networks = {"media": {"external": True, "name": f"{cfg['project_name']}-media"}}
    if cfg["options"]["wud"]:
        networks["monitor"] = {"internal": True}
        services["socket-proxy"] = {
            "image": manifest["socket-proxy"], "restart": "unless-stopped", "networks": ["monitor"],
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"],
            "environment": {"CONTAINERS": "1", "IMAGES": "1", "INFO": "1", "VERSION": "1", "EVENTS": "1", "POST": "0"},
        }
    if vpn:
        settings = cfg["vpn"]
        environment = {"VPN_SERVICE_PROVIDER": settings["provider"], "VPN_TYPE": settings["type"],
                       "VPN_PORT_FORWARDING": "on" if settings.get("port_forwarding") else "off",
                       "TZ": cfg["timezone"], "FIREWALL_OUTBOUND_SUBNETS": cfg["network_subnet"],
                       "FIREWALL_INPUT_PORTS": ",".join(str(ctx.container_port(name)) for name in VPN_WEB_SERVICES)
                       + (",8191" if cfg["options"]["byparr"] else ""),
                       "VPN_PORT_FORWARDING_UP_COMMAND": "/bin/sh -c 'echo {{PORT}} > /tmp/gluetun/active-port'",
                       "VPN_PORT_FORWARDING_DOWN_COMMAND": "/bin/sh -c 'echo 0 > /tmp/gluetun/active-port'"}
        for key in ["server_countries", "server_cities", "wireguard_private_key", "wireguard_addresses",
                    "wireguard_public_key", "wireguard_endpoint_ip", "wireguard_endpoint_port",
                    "openvpn_user", "openvpn_password"]:
            if settings.get(key):
                environment[key.upper()] = str(settings[key])
        if settings["provider"] == "protonvpn" and settings.get("port_forwarding"):
            environment["PORT_FORWARD_ONLY"] = "on"
        services["gluetun"] = {
            "image": manifest["gluetun"], "restart": "unless-stopped", "cap_add": ["NET_ADMIN"],
            "devices": ["/dev/net/tun:/dev/net/tun"], "environment": environment,
            "networks": {"media": {"aliases": list(ARR_VPN_ALIASES)}},
            "volumes": [f"{state / 'apps/gluetun'}:/gluetun", f"{state / 'apps/gluetun-status'}:/tmp/gluetun"],
            "ports": [f"{host_for(name)}:{cfg['ports'][name]}:{ctx.container_port(name)}" for name in VPN_WEB_SERVICES],
        }
        if settings.get("port_forwarding"):
            services["port-sync"] = {
                "image": manifest["port-sync"], "restart": "unless-stopped", "network_mode": "service:gluetun",
                "depends_on": {"gluetun": {"condition": "service_healthy", "restart": True},
                               "qbittorrent": {"condition": "service_started"}},
                "command": ["python", "/app/port-sync.py"], "read_only": True,
                "security_opt": ["no-new-privileges:true"], "cap_drop": ["ALL"],
                "volumes": [f"{SOURCE_ROOT / 'docker/port-sync.py'}:/app/port-sync.py:ro",
                            f"{state / 'port-sync.json'}:/run/secrets/port-sync.json:ro",
                            f"{state / 'apps/gluetun-status'}:/status:ro"],
            }
    return {"name": cfg["project_name"], "services": services, "networks": networks}


def write_compose(ctx: Context, *, bootstrap: bool = False) -> None:
    if ctx.cfg["vpn"]["mode"] != "none" and ctx.cfg["vpn"].get("port_forwarding"):
        write_private(ctx.state / "port-sync.json", json.dumps({
            "username": ctx.cfg["admin"]["username"], "password": ctx.cfg["admin"]["password"],
            "port": ctx.container_port("qbittorrent"),
        }))
    write_private(ctx.state / "compose.json", json.dumps(_literal(render(ctx, bootstrap=bootstrap)), indent=2) + "\n")
