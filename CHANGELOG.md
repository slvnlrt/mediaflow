# Notes de version

## 0.3.0

- Installation depuis Git : `bash setup.sh` vérifie les prérequis Linux, propose d'installer les paquets manquants et lance l'assistant de configuration.
- Comptes locaux et connexions entre Jellyfin, Seerr, Radarr, Sonarr, Prowlarr et qBittorrent ; profils de qualité et formats personnalisés synchronisés avec Recyclarr.
- VPN facultatif via Gluetun ; QUI, Byparr et WUD activables séparément.
- Mises à jour des images sur confirmation, sauvegarde préalable et restauration des données et images précédentes en cas d'échec. Les sauvegardes contiennent des secrets, ne sont pas chiffrées et excluent les médias.

### Compatibilité

La préparation des prérequis a été testée sur Debian 12/13 et Ubuntu 24.04 en amd64. Le routage WireGuard et le blocage des sorties directes ont été testés avec un pair indépendant ; la connexion à ProtonVPN et l'attribution d'un port par le fournisseur ne sont pas qualifiées. Les comptes de trackers privés et le transcodage matériel dépendent de votre installation.
