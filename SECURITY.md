# Sécurité et confidentialité

## Frontière public / privé

Les sources ne contiennent aucune base applicative, identité, credential ou donnée de bibliothèque. Le catalogue audiovisuel distribué contient uniquement des règles partageables : profils, formats, scores, tailles et renommage, sans identifiants de base propres à un serveur.

Tout l'état généré vit sous `.state/` : configuration d'installation, credentials, clés, cookies applicatifs, journaux et bases. Ce répertoire est privé et ignoré par Git. Les fichiers de configuration du bootstrap sont créés en mode 0600 dans un répertoire 0700.

Les sauvegardes contiennent ces secrets. Elles sont privées, médias exclus, et ne doivent pas être jointes à un ticket, copiées dans le dépôt ni envoyées à un outil de diagnostic partagé. Le contrôle d'accès local ne remplace pas le chiffrement d'un support de sauvegarde externe.

Ne partagez jamais un `docker inspect`, un `docker compose config`, un fichier `.env`, une configuration JSON privée ou des logs applicatifs bruts : ils peuvent révéler les secrets résolus.

## Réseau

Les nouvelles interfaces de création de compte restent liées à loopback pendant le provisioning. Après configuration, les ports utilisent l'adresse explicitement choisie. Le dépôt n'ouvre ni routeur ni pare-feu.

Les interfaces d'administration doivent rester sur un réseau de confiance. HTTP sur le LAN n'est pas du chiffrement : n'utilisez pas cette configuration directement sur un réseau partagé ou hostile. Un proxy TLS et un contrôle d'accès distant sont hors du parcours standard.

Gluetun protège seulement les services documentés dans le guide VPN. Ne désactivez pas son pare-feu et ne lancez pas le client torrent sur un autre réseau pour contourner un échec.

WUD consulte Docker au travers d'un proxy limité aux lectures nécessaires, sans déclencheur de mise à jour. Un montage `docker.sock:ro` seul ne constitue pas une API Docker en lecture seule.

## Maintenance

Les images livrées sont épinglées par digest. Une mise à jour de code n'est pas une autorisation de remplacer silencieusement les images par les derniers tags.

`update --check` consulte les registres amont. `update` demande confirmation des nouveaux digests avant de les appliquer ; `--yes` donne cet accord explicitement sans interaction. Les tags suivis sont définis dans `images.json`. L'épinglage garantit que le digest sélectionné est celui déployé, pas que toute nouvelle version amont est compatible ou auditée.

Les mots de passe choisis au premier lancement ne sont pas réinitialisés lors d'une relance. Si vous les changez ensuite dans une application, mettez à jour la configuration privée correspondante avant un nouveau provisioning ; un refus d'authentification doit être résolu, pas contourné par un reset automatique.

Pour signaler un problème, fournissez uniquement la version du dépôt, le nom de l'application, le code HTTP ou message d'erreur masqué, et les étapes de reproduction sans données personnelles.
