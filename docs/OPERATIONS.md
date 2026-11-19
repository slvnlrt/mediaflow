# Sauvegarde, restauration et mise à jour

## Sauvegarder

```bash
python3 mediaflow backup
# Ou choisir une autre destination :
python3 mediaflow backup --output /chemin/vers/sauvegardes-privees
# Par défaut : ~/mediaflow-backups/ (dans le dossier personnel du compte exécutant la commande).
```

La sauvegarde arrête temporairement les services pour obtenir des bases cohérentes, puis remet en marche les services précédemment actifs. Elle contient l'état privé des applications, comptes et paramètres, y compris Jellyfin et les options activées. Les médias ne sont pas inclus.

L'archive est créée en mode 0600. **Elle contient des secrets et n'est pas chiffrée par MediaFlow.** Stockez-la sur un support privé/chiffré et conservez-en une copie hors du disque du serveur. Une archive sur le même disque ne protège pas contre la perte de ce disque.

Le cache Jellyfin peut être régénéré et n'est pas nécessaire à la restauration. Les fichiers de bibliothèque doivent être sauvegardés séparément si vous souhaitez les conserver.

## Restaurer sur le serveur actuel

```bash
python3 mediaflow restore /chemin/vers/archive.tar.gz
```

La commande vérifie l'archive avant de remplacer les configurations. Elle demande une confirmation, conserve les paramètres explicites de destination, puis laisse les services **arrêtés** pour vous permettre de contrôler les chemins.

```bash
python3 mediaflow start
python3 mediaflow doctor
```

Ne mélangez pas manuellement les bases de plusieurs sauvegardes et ne réutilisez pas un ancien binaire sur une base migrée sans restauration cohérente.

## Restaurer sur une machine vierge

1. Récupérez les sources MediaFlow compatibles avec la sauvegarde.
2. Depuis leur dossier, lancez `bash setup.sh --prepare-only` : cela prépare Python et Docker sans créer de nouvelle stack ni de comptes à la place de votre sauvegarde.
3. Montez/restaurez vos médias à l'emplacement choisi. L'archive des configurations ne contient pas les films et séries.
4. Créez un fichier `destination.local.json` contenant les paramètres de la **nouvelle** machine, par exemple :

```json
{
  "project_name": "mediaflow",
  "data_path": "/srv/mediaflow-data",
  "bind_ip": "127.0.0.1",
  "uid": 1000,
  "gid": 1000
}
```

`127.0.0.1` limite volontairement l'accès au serveur. Remplacez-le par son IP LAN si nécessaire. Adaptez aussi les UID/GID. Le fichier peut ajouter un objet `ports` complet si les ports habituels sont occupés.

```bash
chmod 600 destination.local.json
python3 mediaflow restore /chemin/vers/archive.tar.gz --config destination.local.json
python3 mediaflow start
python3 mediaflow doctor
```

Les comptes et secrets proviennent de la sauvegarde, pas d'un nouvel assistant qui les réinitialiserait. Le changement de projet/chemin/adresse doit être explicite ; les chemins absolus de l'ancien hôte ne sont pas appliqués silencieusement.

## Mettre à jour

Le code source MediaFlow et les images des applications sont deux axes de mise à jour distincts.

### Code source

1. Sauvegardez les configurations et conservez les sources de la version en cours.
2. Récupérez une nouvelle version depuis la source que vous avez choisie et vérifiée, sans remplacer `.state/`.
3. Lisez les changements et migrations éventuels.
4. Si la version demande de réappliquer des réglages applicatifs, lancez `python3 mediaflow install` depuis le dossier existant, puis `python3 mediaflow doctor`. Une mise à jour limitée à la documentation ne nécessite pas de redéploiement.

Remplacer les sources seul ne change pas les images en cours d'exécution : chaque service est épinglé par digest dans l'état privé de l'installation. Les commandes `start`, `install` et `options` n'appliquent pas silencieusement de nouvelles images suite à une mise à jour des sources.

### Images des applications

```bash
python3 mediaflow update --check
```

La commande résout les tags de suivi de chaque service (par exemple `:latest`, `:8`) vers le digest actuellement publié sur le registre, puis compare avec les images réellement déployées. `--check` affiche le plan sans télécharger les couches, modifier la configuration applicative ni redémarrer les services.

```bash
python3 mediaflow update
python3 mediaflow update --yes
python3 mediaflow update --service radarr --service sonarr
```

Sans `--check`, la commande télécharge les nouvelles images **avant d'arrêter les services**, effectue une sauvegarde cohérente, puis redéploie avec les nouveaux digests et vérifie la disponibilité des API. `--yes` supprime la confirmation interactive. `--service` limite la mise à jour aux services nommés (répétable). Si aucune image n'a changé, la commande le signale et ne touche à rien.

**Pins testés vs amont non qualifié.** Les images livrées dans `images.json` sont des pins testés pour cette version de MediaFlow. La commande `update` résout les tags de suivi vers le contenu **actuel du registre amont**, qui n'a pas été validé avec cette version. Une mise à jour de schéma ou de comportement d'une application peut nécessiter des adaptations. Consultez les notes de version amont avant de mettre à jour en production.

WUD signale les versions disponibles mais ne les installe pas automatiquement. Les images restent épinglées par digest : un tag amont qui change ne modifie pas votre installation sans exécution explicite de `update`.

Les canaux suivis sont les tags de `images.json`, sans leur digest ; l'objet `tracking` permet un choix différent. WUD suit `getwud/wud:latest`, pas le tag figé de première installation. Recyclarr reste sur le canal `:8` et le synchroniseur de port sur `python:3.13-alpine` ; les nouvelles images de ces canaux sont découvertes, pas les nouvelles branches majeures. Pour changer de canal, modifiez explicitement `tracking` puis consultez `update --check`. Les canaux `latest` peuvent inclure une nouvelle version majeure.

Même avec `--service`, la sauvegarde cohérente arrête temporairement la stack entière. Aucun nettoyage d'anciennes images n'est effectué. Les images des options désactivées restent épinglées mais ne sont pas recherchées tant que ces options ne sont pas actives.

Docker buildx (`docker-buildx-plugin`) est requis pour la résolution des digests auprès des registres.

## Retour arrière

Un retour au code source précédent ne suffit pas si une application a migré sa base. Le retour arrière doit conserver ensemble les images précédentes et la sauvegarde de données prise avant migration.

La commande `update` tente un retour arrière automatique en cas d'échec du déploiement ou de la vérification applicative : les données et les images précédentes sont restaurées à partir de la sauvegarde créée juste avant. Conservez cette sauvegarde jusqu'à validation du fonctionnement normal ; ne faites pas de `docker image prune` pour résoudre une mise à jour échouée.
