# VPN facultatif

## Choisir son mode

L'assistant propose `none`, `proton` ou `custom`.

Sans VPN, Gluetun n'est pas lancé et aucun identifiant VPN n'est demandé. Ce mode expose votre IP publique aux services contactés. Il ne signifie pas que les interfaces web sont publiées sur Internet.

Avec VPN, **qBittorrent, Prowlarr, Radarr, Sonarr et Byparr (si activé)** utilisent le tunnel Gluetun, y compris pour les métadonnées de Radarr et Sonarr. Si le tunnel tombe, ces applications n'ont pas de sortie Internet directe. Jellyfin, Seerr, Recyclarr, QUI et WUD ne sont pas placés dans ce tunnel.

Les ports internes `7878` et `8989` appartiennent à Radarr et Sonarr dans le réseau partagé. Ne les attribuez pas à l'interface web de qBittorrent, même si vous utilisez d'autres ports pour accéder aux applications depuis le LAN.

## ProtonVPN

Utilisez une configuration propre à cette installation, pas celle d'un autre serveur en cours d'utilisation.

### WireGuard

1. Connectez-vous au portail Proton et générez une configuration WireGuard Linux.
2. Pour du port forwarding, choisissez un abonnement/serveur compatible et activez **NAT-PMP** lors de la génération.
3. Donnez à l'assistant la clé privée et l'adresse WireGuard figurant dans cette configuration.
4. Choisissez éventuellement un pays. Un filtre trop restrictif peut empêcher toute connexion.
5. N'activez le port forwarding que si votre configuration le permet.

### OpenVPN

Utilisez les identifiants **OpenVPN dédiés**, pas votre mot de passe de compte Proton. Le port forwarding nécessite un serveur/abonnement compatible et le suffixe `+pmp` sur l'identifiant OpenVPN.

Références : [Proton avec Gluetun](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/protonvpn.md), [WireGuard Proton](https://protonvpn.com/support/wireguard-configurations/).

## Port forwarding et sécurité

Le port entrant est attribué par le fournisseur. MediaFlow le synchronise vers qBittorrent par une API **authentifiée**, sans désactiver l'authentification localhost.

Lorsqu'aucun port VPN actif n'est disponible, le synchroniseur restreint l'interface torrent à loopback. Le pare-feu de Gluetun reste la protection contre les sorties directes, indépendamment du synchroniseur. Aucune ouverture de port n'est demandée au routeur domestique.

Ne remplacez pas cette protection par une whitelist d'authentification couvrant tout votre LAN ou tout le réseau Docker.

## Autres fournisseurs

Le mode `custom` signifie « fournisseur Gluetun choisi par l'utilisateur », pas « compatibilité vérifiée ». Fournissez son identifiant Gluetun, le protocole et les paramètres requis.

Le support du VPN n'implique pas celui du port forwarding. Mullvad, par exemple, [ne le propose plus](https://mullvad.net/en/blog/removing-the-support-for-forwarded-ports). Les fournisseurs/protocoles non pris en charge n'ont pas de fallback silencieux.

Les besoins propres à un fournisseur qui ne sont pas proposés par l'assistant doivent être qualifiés avant usage ; ne supprimez pas les vérifications pour forcer un démarrage.

## Qualification

Proton est la cible de qualification de cette stack, pas une garantie pour tous les fournisseurs. Les résultats effectivement obtenus pour la version livrée sont indiqués dans les notes de version. Une validation de syntaxe Compose ou une simple réponse HTTP ne prouve ni le tunnel ni l'absence de fuite.

Pour un essai réel, utilisez un profil VPN de test distinct et contrôlez le chemin réseau pendant une coupure/reconnexion. Ne modifiez pas les credentials d'un serveur actif pour réaliser ce test.
