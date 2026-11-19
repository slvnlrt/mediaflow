# Dépannage

## Le lanceur de prérequis s'arrête

Depuis le dossier MediaFlow, lancez `bash setup.sh --check`. Il indique ce qui manque au **compte courant**, sans installer de paquet ni demander `sudo`. Si l'installation a été faite avec sudo, faites aussi le contrôle avec `sudo bash setup.sh --check`.

- **Distribution non gérée** : la préparation APT est réservée à Debian 12/13 et Ubuntu 24.04 (amd64/arm64). Sur un autre Linux, fournissez Python 3.11+, les fuseaux horaires, un moteur Docker Linux local, Compose avec `up --wait-timeout` et Buildx avec `imagetools inspect --format`. Relancez le contrôle.
- **Paquet en conflit, moteur existant sans CLI, plugin indisponible** : le lanceur ne désinstalle rien pour forcer Docker CE. Conservez la famille de paquets de votre installation, ou faites planifier sa migration par l'administrateur. Voir les instructions officielles pour [Debian](https://docs.docker.com/engine/install/debian/) ou [Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Ne purgez pas `/var/lib/docker`.
- **APT ou signature en erreur** : vérifiez la connexion et l'horloge de l'hôte. Ne désactivez pas les signatures. Si la clé de Docker a changé, utilisez une version de MediaFlow à jour après vérification indépendante. Les paquets déjà installés restent présents ; relancez après correction.
- **Docker ne démarre pas** : sur systemd, consultez `systemctl status docker`. Un contexte SSH/TCP n'est pas accepté. Un moteur rootless doit être démarré par son propriétaire. Ne rendez pas `/var/run/docker.sock` accessible en écriture à tous.

## L'installation échoue

Ne supprimez pas `.state/` pour recommencer : ce dossier contient les comptes et bases déjà créés.

1. Notez le composant et le code d'erreur affichés, sans copier de secret.
2. Corrigez la cause.
3. Relancez `python3 mediaflow install` depuis le même dossier.

La relance réutilise les comptes et les objets qu'elle a créés. Elle ne doit pas écraser une installation étrangère.

## Docker introuvable ou accès refusé

Vérifiez `docker info` et `docker compose version` sous le **même compte Linux** que celui de l'installation. Une erreur de socket n'est pas une raison pour lancer les applications en UID 0. Si vous utilisez root pour l'installateur, indiquez explicitement l'UID/GID non-root du propriétaire des médias et utilisez ensuite le même mode d'administration.

## Impossible d'ouvrir une interface depuis mon PC

- Utilisez l'IP du **serveur**, pas `localhost` sur votre PC.
- Une installation liée à `127.0.0.1` reste locale au serveur.
- Avant la fin du provisioning, les nouvelles interfaces restent volontairement sur loopback.
- Vérifiez le pare-feu Linux et que votre PC est sur le LAN autorisé. N'ouvrez pas de redirection Internet pour résoudre ce problème.

## Un port est occupé

Choisissez des ports hôte distincts dans la configuration JSON privée avant le premier lancement. Les ports internes des applications sont gérés par MediaFlow ; les changer manuellement casse leurs connexions.

## Erreur de permissions ou de hardlinks

Le dossier des données doit appartenir au compte média ou lui être accessible en écriture. L'installateur ne fait pas de `chown -R` arbitraire sur une médiathèque existante.

Les téléchargements et la bibliothèque doivent être sur le même système de fichiers. Un dossier `torrents` et un dossier `media` qui pointent vers deux montages différents ne satisfont pas cette condition, même s'ils ont un parent commun.

## VPN indisponible

Vérifiez les paramètres fournisseur et la disponibilité de `/dev/net/tun`. Pour Proton WireGuard avec port forwarding, vérifiez l'option NAT-PMP de la configuration générée. Ne désactivez pas le pare-feu Gluetun et ne remplacez pas le réseau du client torrent par une connexion directe.

Les autres fournisseurs Gluetun ne sont pas qualifiés. Un fournisseur sans port forwarding doit être utilisé avec cette option désactivée.

## Aucun résultat ou téléchargement

Vérifiez vos indexeurs dans Prowlarr et leurs accès. Une application saine sans indexeur ne peut pas trouver de contenu. Les restrictions du profil FR 1080p peuvent aussi exclure certaines versions. Ne lancez pas de recherche globale uniquement pour vérifier l'installation.

Si QUI a été activé après l'ajout d'indexeurs, relancez l'installation pour rapprocher la configuration gérée. Les règles, invitations, abonnements et protections interactives des trackers restent propres à chaque utilisateur.

## Diagnostic privé

```bash
python3 mediaflow doctor
python3 mediaflow plan
```

Les erreurs Compose détaillées, si présentes, sont stockées dans `.state/last-compose-error.log` avec permissions privées. Lisez-les localement ; elles peuvent contenir des données résolues. Ne partagez jamais ce fichier brut, les logs applicatifs, `docker inspect` ou `docker compose config`.

## Installation non interactive

Utilisez un fichier JSON privé en mode 0600 et la même structure que la configuration de l'assistant. Le mot de passe doit être fourni dans ce fichier, jamais dans les arguments du processus.

```bash
chmod 600 installation.local.json
python3 mediaflow install --config installation.local.json --yes --accept-no-vpn
```

`--accept-no-vpn` n'est nécessaire que pour une première installation non interactive sans tunnel. Il confirme explicitement l'usage de l'IP publique ; il ne désactive jamais un VPN configuré.

L'option `--config` est réservée à la première installation. Pour reprendre une installation existante, utilisez sa configuration privée `.state/config.json`, sans la remplacer par celle d'un autre serveur.

## Interfaces accessibles mais métadonnées indisponibles après reboot

Une réponse HTTP locale ne prouve pas la résolution DNS externe. Le réseau et le résolveur de l'hôte doivent être prêts avant le démarrage de Docker. Sur certains hôtes DHCP/ifupdown, atteindre `network-online.target` ne garantit pas qu'un bail et un DNS soient déjà disponibles.

Vérifiez le DNS dans l'application concernée et la configuration réseau de votre distribution. Si nécessaire, configurez une attente réseau effective ou un DNS Docker adapté à votre réseau, avec sauvegarde et redémarrage planifié. MediaFlow ne modifie pas silencieusement ces réglages globaux et ne fournit aucune adresse DNS personnelle du mainteneur.
