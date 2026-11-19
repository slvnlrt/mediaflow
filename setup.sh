#!/usr/bin/env bash
# Linux entry point. Never remove packages, switch Docker contexts or grant Docker group access.
set -euo pipefail
fail() { printf 'Arrêt : %s\n' "$*" >&2; exit 1; }
confirm() { local reply; read -r -p "$1 [o/N] " reply && [[ $reply == o || $reply == O || $reply == oui ]] || fail 'annulé, aucune action supplémentaire.'; }
usage() { printf '%s\n' 'bash setup.sh                Vérifier, préparer, puis lancer MediaFlow.' 'bash setup.sh --check        Diagnostic seul, sans sudo ni modification.' 'bash setup.sh --prepare-only Préparer la machine sans déployer MediaFlow.'; }
mode=${1:-install}
[[ $# -le 1 ]] || { usage; exit 2; }
case "$mode" in --help|-h) usage; exit 0;; install|--check|--prepare-only) ;; *) usage; exit 2;; esac
[[ $(uname -s) == Linux && $(getconf LONG_BIT) == 64 ]] || fail 'un serveur Linux 64 bits est nécessaire.'
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
[[ -f mediaflow && -d mediaflow_lib ]] || fail 'relancez bash setup.sh depuis un clone complet du dépôt MediaFlow.'

# Honor the effective Docker context before any privileged action, including when
# DOCKER_CONTEXT overrides DOCKER_HOST. Never silently replace a remote endpoint.
endpoint=${DOCKER_HOST:-unix:///var/run/docker.sock}
if command -v docker >/dev/null && { [[ -n ${DOCKER_CONTEXT:-} ]] || [[ -z ${DOCKER_HOST:-} ]]; }; then
    endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}') || fail 'contexte Docker illisible.'
elif [[ -n ${DOCKER_CONTEXT:-} ]]; then
    fail 'contexte Docker explicite sans CLI disponible ; utilisez un contexte Linux local.'
fi
[[ $endpoint == unix:///* ]] || fail 'contexte Docker distant refusé. Sélectionnez explicitement votre moteur Linux local.'

has_python() { python3 -c 'import sys; from zoneinfo import ZoneInfo; ZoneInfo("Europe/Paris"); sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; }
has_compose() { local help; help=$(docker compose up --help 2>/dev/null) && [[ $help == *--wait-timeout* ]]; }
has_buildx() { local help; help=$(docker buildx imagetools inspect --help 2>/dev/null) && [[ $help == *--format* ]]; }
engine() { [[ $(timeout 15 "$@" docker --host "$endpoint" info --format '{{.OSType}}' 2>/dev/null) == linux ]]; }
missing=0
report() { if "$2"; then printf 'OK      %s\n' "$1"; else printf 'MANQUE  %s\n' "$1"; missing=1; fi; }
report 'Python 3.11+ avec fuseaux horaires' has_python
report 'Moteur Docker Linux local accessible à ce compte' engine
report 'Docker Compose avec attente de démarrage' has_compose
report 'Docker Buildx avec inspection des digests' has_buildx
if [[ $mode == --check ]]; then
    ((missing == 0)) || fail 'préparation nécessaire : relancez bash setup.sh sans --check.'
    exit 0
fi

admin=()
need_admin() {
    if (( EUID != 0 )); then command -v sudo >/dev/null || fail 'sudo absent : demandez à l’administrateur de lancer la préparation.'; admin=(sudo); fi
}
installed() { [[ $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || :) == 'install ok installed' ]]; }
apt_install() { "${admin[@]}" env DEBIAN_FRONTEND=noninteractive apt-get -y --no-install-recommends --no-remove install "$@"; }
empty_or_absent() {
    local first
    [[ ! -L $1 ]] || return 1
    [[ -e $1 ]] || return 0
    [[ -d $1 ]] || return 1
    first=$(find "$1" -mindepth 1 -maxdepth 1 -print -quit) || return 1
    [[ -z $first ]]
}
if ! has_python || ! command -v docker >/dev/null || ! has_compose || ! has_buildx; then
    [[ -r /etc/os-release ]] || fail 'distribution inconnue ; installez les prérequis manuellement.'
    . /etc/os-release
    case "${ID:-}:${VERSION_ID:-}:${VERSION_CODENAME:-}" in debian:12:bookworm|debian:13:trixie|ubuntu:24.04:noble) ;; *) fail 'installation automatique réservée à Debian 12/13 et Ubuntu 24.04. Préparez les outils manuellement sur les autres Linux.';; esac
    arch=$(dpkg --print-architecture)
    [[ $arch == amd64 || $arch == arm64 ]] || fail 'préparation automatique disponible uniquement pour amd64/arm64.'
    packages=(); new_engine=false; docker_packages=false
    has_python || packages+=(python3 tzdata)
    if ! command -v docker >/dev/null; then
        if command -v dockerd >/dev/null || ! empty_or_absent /var/lib/docker; then
            fail 'moteur ou données Docker existants sans CLI utilisable. Réparez cette installation ; rien ne sera remplacé.'
        fi
        [[ ! -S ${endpoint#unix://} ]] || fail 'socket Docker présent sans CLI ; installation existante à réparer manuellement.'
        for pkg in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc moby-engine; do
            if installed "$pkg"; then fail "paquet existant $pkg incompatible avec Docker CE : rien ne sera désinstallé. Consultez docs/TROUBLESHOOTING.md."; fi
        done
        new_engine=true; docker_packages=true; packages+=(docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin)
    elif ! has_compose || ! has_buildx; then
        if installed docker-ce-cli; then
            docker_packages=true; has_compose || packages+=(docker-compose-plugin); has_buildx || packages+=(docker-buildx-plugin)
        elif installed docker.io; then
            # Keep the distribution's engine and packages; never mix Docker CE with docker.io.
            has_compose || packages+=(docker-compose-v2); has_buildx || packages+=(docker-buildx)
        else fail 'CLI Docker non gérée par APT. Installez Compose v2/Buildx pour cette CLI ; le moteur existant ne sera pas remplacé.'; fi
    fi
    printf 'Préparation APT : %s\n' "${packages[*]}"
    if $docker_packages; then printf '%s\n' 'Le dépôt stable signé de Docker sera utilisé (ajout si absent).'; fi
    if $docker_packages; then printf '%s\n' 'Au besoin, APT installera aussi ca-certificates, curl et gpg pour vérifier la clé du dépôt.'; fi
    if $new_engine; then printf '%s\n' 'Le service Docker sera installé et démarré. Docker peut créer des règles réseau sur cet hôte.'; fi
    confirm 'Autoriser cette préparation système ?'
    need_admin
    "${admin[@]}" apt-get update --error-on=any
    if $docker_packages && ! grep -qsE '^[^#]*https://download\.docker\.com/linux/(debian|ubuntu)' /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; then
        key=/etc/apt/keyrings/mediaflow-docker.asc; source=/etc/apt/sources.list.d/mediaflow-docker.sources
        [[ ! -e $source && ! -L $source ]] || fail 'fichier de dépôt tiers déjà présent ; aucun écrasement.'
        apt_install ca-certificates curl gpg
        tmp=$(mktemp -d); trap 'rm -rf -- "$tmp"' EXIT
        curl --fail --silent --show-error --proto '=https' "https://download.docker.com/linux/$ID/gpg" -o "$tmp/key"
        fingerprint=$(gpg --homedir "$tmp" --batch --show-keys --with-colons "$tmp/key" | awk -F: '$1=="pub"{primary=1} $1=="fpr" && primary{print $10; primary=0}')
        [[ $fingerprint == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || fail 'empreinte de la clé Docker inattendue ; dépôt non ajouté.'
        "${admin[@]}" install -d -m 0755 /etc/apt/keyrings
        if [[ -e $key || -L $key ]]; then cmp -s "$key" "$tmp/key" || fail 'clé existante différente ; aucun écrasement.'; else "${admin[@]}" install -m 0644 "$tmp/key" "$key"; fi
        printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: %s\n' "$ID" "$VERSION_CODENAME" "$arch" "$key" > "$tmp/source"
        "${admin[@]}" install -m 0644 "$tmp/source" "$source"
        rm -rf -- "$tmp"; trap - EXIT
        "${admin[@]}" apt-get update --error-on=any
    fi
    if ! apt-get -s --no-remove --no-install-recommends install "${packages[@]}" >/dev/null; then fail 'paquets indisponibles ou conflit APT. Aucun moteur existant ne sera remplacé ; consultez docs/TROUBLESHOOTING.md.'; fi
    apt_install "${packages[@]}"
    has_python && has_compose && has_buildx || fail 'outil encore incompatible (vérifiez PATH, python3, docker compose et docker buildx).'
fi

# Access denied is not a stopped service. Probe as administrator before proposing a start.
run=()
if ! engine; then
    case "$endpoint" in unix:///var/run/docker.sock|unix:///run/docker.sock) ;; *) fail 'socket personnalisé inaccessible. Démarrez votre moteur Docker local avec son propriétaire.';; esac
    need_admin
    if (( EUID != 0 )); then confirm 'Tester le socket local avec sudo, sans ajouter votre compte au groupe docker ?'; sudo -v; fi
    if ! engine "${admin[@]}"; then
        confirm 'Docker ne répond pas. Démarrer le service Docker local (sans redémarrer un moteur actif) ?'
        if [[ -d /run/systemd/system ]]; then "${admin[@]}" systemctl start docker
        elif command -v service >/dev/null && [[ -x /etc/init.d/docker ]]; then "${admin[@]}" service docker start
        else fail 'pas de gestionnaire de service Docker disponible. Démarrez le moteur sur le serveur puis relancez.'; fi
        for ((i=0;i<15;i++)); do engine "${admin[@]}" && break; sleep 1; done
        engine "${admin[@]}" || fail 'Docker reste indisponible ; consultez les journaux du service Docker.'
    fi
    if ! engine; then run=(sudo); printf '%s\n' 'Le socket exige sudo. Utilisez aussi sudo pour les futures commandes MediaFlow ; aucun groupe ni permission du socket modifié.'; fi
fi
printf '%s\n' 'Prérequis disponibles. Aucun média ni conteneur applicatif créé par la préparation.'
if [[ $mode == --prepare-only ]]; then exit 0; fi
if [[ -e .state && ! -r .state/config.json && $EUID != 0 && ${#run[@]} == 0 ]]; then fail 'état privé inaccessible : reprenez avec le compte qui a installé MediaFlow (ou sudo si utilisé auparavant).'; fi
if ((${#run[@]})); then
    # Use the exact socket tested above even if root has its own Docker context.
    confirm 'Autoriser le lancement de l’assistant MediaFlow avec sudo sur ce serveur ?'
    sudo python3 -c 'import sys; from zoneinfo import ZoneInfo; ZoneInfo("Europe/Paris"); sys.exit(sys.version_info < (3, 11))' || fail 'Python 3.11+ indisponible pour sudo.'
    sudo docker compose version >/dev/null && sudo docker buildx version >/dev/null || fail 'Compose/Buildx indisponibles pour sudo.'
    exec sudo env DOCKER_HOST="$endpoint" python3 mediaflow install
fi
unset DOCKER_CONTEXT
export DOCKER_HOST="$endpoint"
exec python3 mediaflow install
