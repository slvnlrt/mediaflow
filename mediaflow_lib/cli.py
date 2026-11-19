"""Single entry point for installation, checks and reversible maintenance."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

from .common import Context, ProvisionError, write_private
from .compose import SOURCE_ROOT, ensure_network, write_compose
from .config import DEFAULT_PORTS, initialize_secrets, interactive, preflight, prepare, validate


def _load(path: Path) -> dict:
    if path.is_symlink():
        raise ProvisionError("Refusing a symlinked configuration")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProvisionError("Private configuration is too permissive; run chmod 600 on it")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise ProvisionError("Cannot read the private JSON configuration") from None
    if not isinstance(value, dict):
        raise ProvisionError("Configuration must be a JSON object")
    return value


def _services(cfg: dict) -> list[str]:
    names = ["qbittorrent", "radarr", "sonarr", "prowlarr", "jellyfin", "seerr", "recyclarr"]
    names += [name for name, enabled in cfg["options"].items() if enabled]
    return names


def _plan(cfg: dict) -> None:
    print("Services : " + ", ".join(_services(cfg)))
    print("Données : " + cfg["data_path"])
    print("Accès final : " + cfg["bind_ip"] + " ; aucun changement du routeur/pare-feu")
    print("VPN : " + cfg["vpn"]["mode"])
    if cfg["vpn"]["mode"] == "none":
        print("ATTENTION : connexions sortantes directes via votre IP publique.")
    elif cfg["vpn"]["mode"] == "custom":
        print("Fournisseur non qualifié : compatibilité et port forwarding à vérifier.")
    print("Identifiants et bases : .state/ (privé, jamais à publier)")


def _provision(ctx: Context) -> None:
    # Import provisioners lazily so --help and preflight need no running applications.
    from .arr import configure_qbittorrent, provision_arr, configure_recyclarr
    from .media import configure_jellyfin, configure_seerr, configure_qui, configure_wud

    ctx.note("Configuration qBittorrent…")
    configure_qbittorrent(ctx)
    if ctx.cfg["options"]["qui"]:
        ctx.note("Configuration QUI et du proxy…")
        configure_qui(ctx)
    ctx.note("Configuration Radarr, Sonarr et Prowlarr…")
    profiles = provision_arr(ctx)
    ctx.note("Application des profils et formats audiovisuels…")
    configure_recyclarr(ctx)
    ctx.note("Configuration Jellyfin et des bibliothèques…")
    configure_jellyfin(ctx)
    ctx.note("Configuration Seerr…")
    configure_seerr(ctx, profiles)
    if ctx.cfg["options"]["wud"]:
        ctx.note("Vérification WUD authentifié…")
        configure_wud(ctx)


def _adopt_pins(ctx: Context) -> bool:
    """Ensure every active service has a durable image pin before compose render."""
    from .image_updates import active_services, ensure_pins
    from .maintenance import snapshot_images
    active = active_services(ctx.cfg)
    portable: dict[str, str] = {}
    if (ctx.state / "compose.json").exists():
        _, portable = snapshot_images(ctx)
    return ensure_pins(ctx.cfg, active, portable)


def _release_legacy_vpn_ports(ctx: Context) -> None:
    # Compose brings Gluetun up first. The old standalone *arr containers must
    # release their host ports before Gluetun can take ownership of those ports.
    path = ctx.state / "compose.json"
    if ctx.cfg["vpn"]["mode"] == "none" or not path.is_file():
        return
    services = json.loads(path.read_text())["services"]
    legacy = [s for s in ("radarr", "sonarr") if services.get(s, {}).get("ports")]
    if legacy:
        ctx.compose("stop", *legacy)


def install(ctx: Context, *, fresh: bool, yes: bool, accept_no_vpn: bool) -> None:
    validate(ctx.cfg, ctx.root)
    _plan(ctx.cfg)
    if ctx.cfg["vpn"]["mode"] == "none" and fresh and yes and not accept_no_vpn:
        raise ProvisionError("Noninteractive installation without VPN requires --accept-no-vpn")
    if not yes and input("Appliquer cette installation ? (o/n) : ").strip().lower() not in {"o", "oui", "y", "yes"}:
        raise ProvisionError("Installation annulée")
    preflight(ctx, fresh=fresh)
    initialize_secrets(ctx.cfg)
    prepare(ctx)
    ensure_network(ctx)
    _adopt_pins(ctx)
    ctx.cfg["phase"] = "provisioning"
    ctx.save()
    _release_legacy_vpn_ports(ctx)
    write_compose(ctx, bootstrap=True)
    ctx.note("Téléchargement des images manquantes de cette version…")
    ctx.compose("pull", "--policy", "missing")
    # Recyclarr runs after API keys and profiles exist; no failing cron during bootstrap.
    names = [name for name in _services(ctx.cfg) if name != "recyclarr"]
    if ctx.cfg["options"]["wud"]:
        names.append("socket-proxy")
    if ctx.cfg["vpn"]["mode"] != "none":
        names.insert(0, "gluetun")
        if ctx.cfg["vpn"].get("port_forwarding"):
            names.append("port-sync")
    ctx.note("Démarrage protégé : les nouvelles interfaces restent sur loopback jusqu'à configuration…")
    ctx.compose("up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "240", *names)
    _provision(ctx)
    ctx.cfg["managed"]["published_services"] = _services(ctx.cfg)
    ctx.save()
    write_compose(ctx, bootstrap=False)
    ctx.compose("up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "240")
    ctx.host = ctx.cfg["bind_ip"] if ctx.cfg["bind_ip"] != "0.0.0.0" else "127.0.0.1"
    doctor(ctx)
    ctx.cfg["phase"] = "ready"
    ctx.save()
    print("Installation configurée. Ajoutez vos propres indexeurs dans Prowlarr.")
    print("Aucun téléchargement, recherche globale ou suppression de média n'a été déclenché.")
    for name in _services(ctx.cfg):
        if name not in {"recyclarr", "byparr"}:
            print(f"  {name}: http://{ctx.cfg['bind_ip']}:{ctx.cfg['ports'][name]}")


def doctor(ctx: Context) -> None:
    rows = ctx.compose("ps", "--all", "--format", "json", capture=True).stdout.strip()
    try:
        parsed = json.loads(rows)
        containers = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        containers = [json.loads(row) for row in rows.splitlines() if row.strip()]
    expected = set(_services(ctx.cfg))
    if ctx.cfg["vpn"]["mode"] != "none":
        expected.add("gluetun")
        if ctx.cfg["vpn"].get("port_forwarding"):
            expected.add("port-sync")
    if ctx.cfg["options"]["wud"]:
        expected.add("socket-proxy")
    found = {row["Service"]: row for row in containers}
    unhealthy = [name for name in expected if name not in found or found[name].get("State") != "running"
                 or found[name].get("Health") in {"unhealthy", "starting"}]
    if unhealthy:
        raise ProvisionError("Services not ready: " + ", ".join(sorted(unhealthy)))
    for app, version in [("radarr", "v3"), ("sonarr", "v3"), ("prowlarr", "v1")]:
        ctx.api(app, headers={"X-Api-Key": ctx.cfg["secrets"][app]}).wait(f"/api/{version}/system/status")
    ctx.api("jellyfin").wait("/health")
    ctx.api("seerr").wait("/api/v1/status")
    from .arr import _qbit_api, _qbit_login
    _qbit_login(ctx, _qbit_api(ctx))
    if ctx.cfg["options"]["qui"]:
        api = ctx.api("qui", headers={"X-Requested-With": "XMLHttpRequest"})
        api.wait("/api/auth/check-setup")
        api.request("POST", "/api/auth/login", {
            "username": ctx.cfg["admin"]["username"], "password": ctx.cfg["admin"]["password"],
        })
        api.request("GET", "/api/instances")
        from .qui import diagnose_indexers
        diagnose_indexers(ctx, api)
    if ctx.cfg["options"]["wud"]:
        from .wud import _authenticate
        ctx.api("wud").wait("/health")
        _authenticate(ctx, ctx.cfg)
    print("Services actifs et API essentielles joignables. Ce contrôle n'effectue aucun téléchargement.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MediaFlow — installation Linux et configuration des applications")
    parser.add_argument("--directory", type=Path, default=SOURCE_ROOT, help="répertoire de l'installation")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install", help="installer ou reprendre la configuration sans réinitialiser les comptes")
    setup.add_argument("--config", type=Path, help="configuration JSON privée (chmod 600), première installation seulement")
    setup.add_argument("--yes", action="store_true")
    setup.add_argument("--accept-no-vpn", action="store_true")
    for name in ["doctor", "start", "stop", "plan"]:
        sub.add_parser(name)
    options = sub.add_parser("options", help="activer/désactiver les options et configurer leurs connexions")
    options.add_argument("--enable", action="append", choices=["qui", "byparr", "wud"], default=[])
    options.add_argument("--disable", action="append", choices=["qui", "byparr", "wud"], default=[])
    options.add_argument("--yes", action="store_true")
    backups = sub.add_parser("backup", help="archive privée cohérente, médias exclus")
    backups.add_argument("--output", type=Path, default=Path.home() / "mediaflow-backups")
    restore = sub.add_parser("restore", help="restaurer une archive dans cette installation, médias exclus")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--config", type=Path, help="paramètres privés de destination pour une restauration sur une nouvelle machine")
    update = sub.add_parser("update", help="rechercher les nouvelles images et les appliquer avec sauvegarde et retour arrière")
    update.add_argument("--yes", action="store_true")
    update.add_argument("--check", action="store_true", help="afficher les mises à jour sans télécharger ni déployer")
    update.add_argument("--service", action="append", dest="services", default=[], help="limiter aux services nommés (répétable)")
    args = parser.parse_args(argv)
    root = args.directory.resolve()
    state = root / ".state"
    try:
        if state.is_symlink():
            raise ProvisionError("Refusing a symlinked private state directory")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        lock_path = root / ".mediaflow.lock"
        if lock_path.is_symlink():
            raise ProvisionError("Refusing a symlinked operation lock")
        with lock_path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProvisionError("Another MediaFlow operation is already running") from None
            current = state / "config.json"
            fresh = not current.exists()
            if args.command == "install":
                if not fresh and args.config:
                    raise ProvisionError("Existing installation: use its private configuration, not --config")
                cfg = _load(current) if not fresh else (_load(args.config) if args.config else interactive())
                cfg.setdefault("ports", DEFAULT_PORTS.copy())
                cfg.setdefault("managed", {})
                ctx = Context(root, cfg)
                if cfg["managed"].get("published_services"):
                    ctx.host = cfg["bind_ip"] if cfg["bind_ip"] != "0.0.0.0" else "127.0.0.1"
                install(ctx, fresh=fresh, yes=args.yes, accept_no_vpn=args.accept_no_vpn)
                return 0
            if fresh and args.command != "restore":
                raise ProvisionError("No installation found; run install or restore first")
            if fresh:
                from .maintenance import inspect_backup
                cfg = inspect_backup(args.archive.resolve())["config"]
                if not args.config:
                    raise ProvisionError("Fresh restore requires --config with explicit destination paths, address, IDs and project name")
            else:
                cfg = _load(current)
            if args.command == "restore" and args.config:
                destination = _load(args.config)
                allowed = {"project_name", "data_path", "bind_ip", "uid", "gid", "ports"}
                if set(destination) - allowed or not {"project_name", "data_path", "bind_ip", "uid", "gid"} <= set(destination):
                    raise ProvisionError("Restore destination must define project_name, data_path, bind_ip, uid and gid; only ports may be added")
                if not fresh and destination["project_name"] != cfg["project_name"]:
                    raise ProvisionError("Use an empty --directory to restore under a different Docker project name")
                cfg.update(destination)
            validate(cfg, root)
            host = cfg["bind_ip"] if cfg["bind_ip"] != "0.0.0.0" else "127.0.0.1"
            ctx = Context(root, cfg, host=host)
            if args.command == "plan":
                _plan(cfg)
            elif args.command == "doctor":
                doctor(ctx)
            elif args.command == "stop":
                ctx.compose("stop")
            elif args.command == "start":
                if cfg.get("phase") == "restored":
                    install(ctx, fresh=False, yes=True, accept_no_vpn=True)
                    return 0
                if cfg.get("phase") != "ready":
                    raise ProvisionError("Initial configuration incomplete; use install to resume")
                ensure_network(ctx)
                if _adopt_pins(ctx):
                    ctx.save()
                _release_legacy_vpn_ports(ctx)
                write_compose(ctx)
                ctx.compose("up", "-d", "--wait", "--wait-timeout", "240")
                doctor(ctx)
            elif args.command == "options":
                from .maintenance import backup
                if set(args.enable) & set(args.disable):
                    raise ProvisionError("An option cannot be enabled and disabled together")
                desired = dict(cfg["options"])
                desired.update({name: True for name in args.enable})
                desired.update({name: False for name in args.disable})
                if desired == cfg["options"]:
                    if cfg.get("phase") != "ready":
                        install(ctx, fresh=False, yes=args.yes, accept_no_vpn=True)
                    else:
                        print("Options inchangées.")
                    return 0
                if not args.yes and input("Sauvegarder puis reconfigurer les options ? (o/n) : ").lower() not in {"o", "oui", "y", "yes"}:
                    raise ProvisionError("Modification annulée")
                backup(ctx, root / "backups")
                cfg["options"] = desired
                ctx.save()
                install(ctx, fresh=False, yes=True, accept_no_vpn=True)
            elif args.command == "backup":
                from .maintenance import backup
                print("Archive privée : " + str(backup(ctx, args.output.resolve())))
            elif args.command == "restore":
                from .maintenance import restore
                if not args.yes and input("Remplacer les configurations applicatives par cette sauvegarde ? (o/n) : ").lower() not in {"o", "oui", "y", "yes"}:
                    raise ProvisionError("Restauration annulée")
                restore(ctx, args.archive.resolve())
                print("Configuration restaurée ; contrôlez les chemins puis lancez : python3 mediaflow start")
            elif args.command == "update":
                from .image_updates import active_services, display_plan, plan_updates, ensure_pins
                from .maintenance import update as update_images, snapshot_images
                if cfg.get("phase") != "ready":
                    raise ProvisionError("Complete installation before updating")
                available = active_services(cfg)
                selected = args.services or None
                if selected:
                    unknown = set(selected) - set(available)
                    if unknown:
                        raise ProvisionError(f"Not in active services: {', '.join(sorted(unknown))}")
                _, portable = snapshot_images(ctx)
                planning_cfg = dict(cfg, image_overrides={**cfg.get("image_overrides", {}), **portable})
                ensure_pins(planning_cfg, available, portable)
                ctx.note("Recherche des nouvelles images auprès des registres…")
                plans = plan_updates(planning_cfg, list(dict.fromkeys(selected or available)))
                print("Plan de mise à jour des images :")
                any_changed = display_plan(plans)
                if args.check:
                    return 0
                if not any_changed:
                    print("Toutes les images sélectionnées sont déjà à jour.")
                    return 0
                if not args.yes:
                    if input("Appliquer ces images avec sauvegarde et retour arrière ? (o/n) : ").strip().lower() not in {"o", "oui", "y", "yes"}:
                        raise ProvisionError("Mise à jour annulée")
                update_images(ctx, plans, verify=doctor)
            return 0
    except ProvisionError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"ERREUR : entrée ou système invalide ({type(exc).__name__}). Aucun secret affiché.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrompu. Les comptes existants sont conservés ; relancez la commande pour reprendre.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
