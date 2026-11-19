# MediaFlow

MediaFlow transforme votre serveur Linux en médiathèque familiale. **Pour regarder un film déjà dans votre bibliothèque, ouvrez Jellyfin. Pour découvrir un film ou demander une saison qui manque, ouvrez Seerr sur votre téléphone.** Les autres outils travaillent en arrière-plan. [Utiliser MediaFlow au quotidien](#au-quotidien).

Le VPN est facultatif. QUI/cross-seed, Byparr et WUD s'activent séparément.

## Quick start — depuis Git

**Avant de commencer :**

- Un serveur **Debian 12/13 ou Ubuntu 24.04**, en amd64 ou arm64, avec accès Internet et un compte pouvant utiliser `sudo` (ou root). **Python et Docker ne sont pas nécessaires pour commencer.**
- **Git** installé sur le serveur. Si `git --version` échoue, sur Debian ou Ubuntu : `sudo apt-get update && sudo apt-get install git`.
- L'**adresse IPv4 locale du serveur** pour accéder aux interfaces depuis votre PC, et un **dossier inscriptible sur le disque destiné aux médias**. Pour le socle, prévoyez au moins 4 Gio de RAM.
- **Seulement si vous souhaitez un VPN** : votre configuration fournisseur. **Seulement pour rechercher du contenu après installation** : vos propres accès à un indexeur. Aucun de ces comptes n'est fourni.

**Sur le serveur**, ouvrez un terminal avec votre compte habituel, puis copiez ce bloc :

```bash
git clone https://github.com/slvnlrt/mediaflow.git ~/mediaflow &&
cd ~/mediaflow &&
bash setup.sh
```

Répondez `o` pour autoriser les prérequis proposés. Dans l'assistant, choisissez le disque et l'IP du **serveur**, créez le compte administrateur et laissez les trois options désactivées pour commencer simplement. À la fin, ouvrez **l'URL Jellyfin affichée** sur votre PC. Si vous avez un doute sur une question, [voici les réponses expliquées](#configurer-mediaflow).

## Ce que fait le lanceur

Si `~/mediaflow` est déjà votre clone, ne le supprimez pas et ne le reclonez pas. Pour reprendre, faites `cd ~/mediaflow && bash setup.sh`. Pour changer de version, suivez [la procédure de maintenance](docs/OPERATIONS.md#code-source).

Le lanceur vérifie **Python 3.11+ et les fuseaux horaires, Docker Engine local, Compose et Buildx**. S'il manque des paquets, il affiche le plan et demande votre accord avant d'utiliser APT et, si nécessaire, le dépôt stable signé de Docker. Sur un système déjà prêt, il passe directement à l'assistant MediaFlow. Vous avez besoin d'Internet pour installer des paquets ou télécharger les images.

- **Droits** : si votre compte n'a pas accès au socket Docker, le lanceur peut vous proposer `sudo`. Il ne vous ajoute jamais au groupe `docker` et ne rend pas le socket accessible à tous. Si vous acceptez d'exécuter MediaFlow avec `sudo`, préfixez aussi les futures commandes `python3 mediaflow …` par `sudo`.
- **Système existant** : aucun paquet concurrent n'est supprimé ; aucun moteur actif n'est remplacé par un autre. Installer ou démarrer Docker peut créer des règles réseau. Pas de changement sur votre routeur. [Si la préparation s'arrête](docs/TROUBLESHOOTING.md#le-lanceur-de-prérequis-sarrête).
- **Autres Linux 64 bits** : si les quatre contrôles passent, le lanceur fonctionne sans installation APT. Sinon, installez manuellement [Docker et ses plugins](https://docs.docker.com/engine/install/) ainsi que Python 3.11+ et `tzdata`, puis relancez.
- **Capacité** : prévoyez au moins 4 Gio de RAM pour le socle, davantage avec Byparr ou le transcodage. Le seuil de 5 Gio libres vérifié au démarrage ne dimensionne pas une médiathèque. Téléchargements et bibliothèque doivent être sur le même système de fichiers.

Pour **vérifier seulement**, depuis le dossier cloné : `bash setup.sh --check` (aucun `sudo`, aucune installation). Pour **préparer l'hôte sans installer la stack** : `bash setup.sh --prepare-only`.

## Configurer MediaFlow

**Que répondre à l'assistant ?**

1. **Données** : si votre disque principal a la place nécessaire, acceptez le dossier `mediaflow-data` proposé dans votre dossier personnel. Si vous avez un disque dédié, indiquez un dossier de ce disque où votre compte peut écrire. C'est ici qu'iront les téléchargements **et** la bibliothèque, pas dans le dossier du logiciel.
2. **Adresse** : pour ouvrir Jellyfin depuis un autre PC ou une TV, entrez l'adresse IPv4 du **serveur** sur votre réseau. Si vous vous êtes connecté en SSH par IP locale, c'est cette même IP. `127.0.0.1` ne permet l'accès que depuis le serveur lui-même.
3. **Compte** : choisissez le nom administrateur, une adresse email pour Seerr et un mot de passe d'au moins 12 caractères. Rien ne s'affiche pendant la saisie du mot de passe ; tapez-le une seconde fois pour confirmer. Choisissez ensuite votre fuseau horaire.
4. **VPN** : `none` utilise votre IP publique et demande confirmation. Si vous voulez passer par Proton, préparez d'abord [votre configuration VPN](docs/VPN.md). Ne choisissez pas `none` pour contourner un échec de connexion VPN.
5. **Options** : pour commencer simplement, répondez **non** à QUI, Byparr et WUD. Vous pourrez les activer plus tard sans tout réinstaller. Vérifiez le récapitulatif avant de confirmer.

Avec un compte Linux ordinaire, l'assistant utilise votre identifiant d'utilisateur et de groupe. Si vous installez en tant que root et qu'il demande un UID/GID, obtenez les valeurs avec `id -u` et `id -g` depuis le **compte propriétaire des médias**, pas depuis root.

Les nouvelles interfaces restent accessibles seulement depuis le serveur pendant leur configuration. Elles sont publiées sur l'adresse choisie **après** initialisation des comptes et connexions.

Le mot de passe choisi est initialement utilisé pour les comptes administrateur provisionnés. Gardez-le dans votre gestionnaire de mots de passe. Les clés techniques sont générées localement ; inutile de les copier d'une application à l'autre.

Une interruption ? Relancez **la même commande** : les comptes et données existants ne sont pas réinitialisés. Une erreur n'est pas une réussite partielle cachée : l'assistant indique l'étape qui manque.

## Au quotidien

### La première fois : préparer les accès

1. Depuis le réseau de la maison, ouvrez l'URL **Jellyfin** affichée à la fin de l'installation. Vérifiez que **Films** et **Séries** existent ; des bibliothèques vides au départ, c'est normal. Dans le tableau de bord Jellyfin, créez un utilisateur **sans droits administrateur** pour chaque proche : chacun gardera son historique et sa progression.
2. Si vous souhaitez rechercher du contenu, ouvrez **Prowlarr → Indexers → Add Indexer**. Un *indexeur* est une source de recherche. Ajoutez une source à laquelle vous avez accès avec vos propres identifiants, puis **Test → Save**. Pas de clé à recopier dans Radarr ou Sonarr. Vous n'avez pas besoin d'un indexeur pour regarder vos propres fichiers déjà présents dans Jellyfin.

### Ce soir : regarder sur la TV avec Jellyfin

Jellyfin offre une expérience de streaming façon Netflix : affiches, fiches, saisons et reprise de lecture. **La différence : le catalogue est le vôtre.** Aucun abonnement de streaming ni catalogue Netflix, Disney+ ou autre n'est inclus.

1. Sur la TV connectée au réseau de la maison, cherchez **Jellyfin** dans le magasin d'applications. Il existe des [clients officiels](https://jellyfin.org/downloads/clients/) pour Android TV / Google TV, Fire TV, LG webOS, Samsung Tizen et Roku. La disponibilité dépend du modèle, de l'année et du pays. Si votre TV n'a pas l'application, un boîtier HDMI compatible peut servir de lecteur.
2. Ouvrez l'application. Si elle ne trouve pas le serveur, saisissez **l'URL Jellyfin complète affichée à l'installation** (port `8096` par défaut). Sur la TV, `localhost` désignerait la TV, pas le serveur.
3. Connectez-vous avec votre compte de visionnage, choisissez un titre dans **Films** ou **Séries** et lancez la lecture. Pas besoin d'ouvrir Radarr, Sonarr ou qBittorrent.

### Pour plus tard : découvrir et demander depuis son téléphone

1. Sur le **Wi-Fi de la maison**, ouvrez l'URL **Seerr** (port `5055` par défaut) dans Safari ou Chrome. Choisissez la connexion **Jellyfin** et utilisez votre compte personnel.
2. Parcourez les suggestions ou recherchez un titre. S'il est déjà disponible, regardez-le dans Jellyfin. Sinon, touchez **Demander**, choisissez les saisons si nécessaire, puis confirmez. Seerr garde la trace de la demande ; il ne lit pas les vidéos.
3. Avec les réglages livrés, un administrateur approuve les demandes des utilisateurs ordinaires dans Seerr. Une fois la demande approuvée, Radarr ou Sonarr cherche une version conforme à vos critères. Le titre devient visible dans Jellyfin **après téléchargement et import**, pas dès l'approbation. N'utilisez que des sources et des contenus que vous êtes autorisé à utiliser.

**Une icône Seerr sur le téléphone ?** Seerr propose une [application web mobile (PWA)](https://docs.seerr.dev/using-seerr/notifications/webpush/) : pas besoin de chercher une application officielle dans un store. Sur iPhone : **Safari → Partager → Sur l'écran d'accueil**. Sur Android : **menu du navigateur → Installer l'application** ou **Ajouter à l'écran d'accueil**, selon le navigateur. Sur le LAN en HTTP, vous pouvez toujours utiliser le navigateur ; l'installation complète en PWA et les notifications peuvent nécessiter HTTPS. N'ouvrez pas de port Internet pour obtenir une icône.

### À quoi sert chaque outil ?

Les ports sont ceux par défaut ; utilisez les URL affichées par votre installation.

| Application | Port | Utilisation |
|---|---:|---|
| Jellyfin | 8096 | Le lecteur : parcourir sa bibliothèque et regarder sur TV, téléphone ou navigateur. |
| Seerr | 5055 | Le catalogue de découverte : demander un titre absent, approuver une demande ou suivre son état. |
| Radarr | 7878 | Le gestionnaire de **films** : cherche via les indexeurs synchronisés par Prowlarr, puis importe et renomme les fichiers terminés. |
| Sonarr | 8989 | L'équivalent pour les **séries** : suit saisons et épisodes, recherche ceux qui manquent et les importe. |
| Prowlarr | 9696 | Le carnet de sources : configurer ou tester les indexeurs une fois pour Radarr et Sonarr. |
| qBittorrent | 8080 | Le moteur de téléchargement et de partage : vérifier la progression ou comprendre pourquoi un transfert est arrêté. |
| Recyclarr | — | Maintient les formats et leurs scores dans Radarr/Sonarr. Automatique, sans interface à ouvrir. |
| Gluetun (VPN) | — | Fait passer les applications concernées par le tunnel VPN et bloque leur sortie Internet directe en cas de coupure. |
| QUI (option) | 7476 | Interface avancée de qBittorrent. Le *cross-seed* permet de partager des fichiers déjà présents sur plusieurs trackers compatibles sans les télécharger à nouveau. |
| Byparr (option) | — | Assistant de Prowlarr pour certains indexeurs qui en ont besoin. Pas d'interface à utiliser au quotidien. |
| WUD (option) | 3000 | Signale les nouvelles images Docker à l'administrateur ; ne les installe pas. |

**Une demande n'arrive pas sur la TV ?** Regardez d'abord son état dans Seerr. Si elle est approuvée, ouvrez **Radarr** (film) ou **Sonarr** (série) : la file d'activité indique si un transfert ou un import attend. Si aucune version n'est trouvée, testez l'indexeur dans Prowlarr et consultez les [critères du profil](docs/QUALITY.md). Si le transfert est lancé mais bloqué, regardez qBittorrent. Le [guide de dépannage](docs/TROUBLESHOOTING.md) décrit aussi `python3 mediaflow doctor`, qui vérifie les services sans demander ni télécharger de contenu.

## Ce qui est configuré

- Catégories qBittorrent `radarr` et `sonarr`, chemins cohérents sous `/data` et dossier temporaire dans le bon volume.
- Pour un envoi manuel depuis Prowlarr, un client qBittorrent direct est configuré sans catégorie, même si Radarr et Sonarr passent par QUI. Les catégories de recherche films et séries sont distinctes ; Sonarr utilise aussi la catégorie anime `5070` avec la recherche au format standard.
- Avec QUI, `python3 mediaflow doctor` signale les différences entre les indexeurs torrent actifs de ce Prowlarr et ses entrées dans QUI. Ce contrôle ne réactive ni ne supprime rien. Pour ajouter dans QUI un indexeur actif manquant, relancez `python3 mediaflow install`.
- Racines films/séries, import terminé, hardlinks et renommage.
- Politique audiovisuelle complète : six profils par application, 26 formats personnalisés Radarr et 15 Sonarr, scores, groupes de qualités, tailles et renommage. **HD-1080p** est le profil utilisé par défaut dans Seerr. [Détail des choix et exclusions](docs/QUALITY.md).
- Bibliothèques Jellyfin et connexions Seerr avec les bons profils/dossiers.
- Authentification, comptes et clés propres à cette installation.

Les torrents ne sont pas supprimés après import : les hardlinks permettent de conserver le seeding sans doubler les blocs de données. Les règles de ratio de vos trackers restent votre responsabilité.

Radarr HD-1080p exige la langue française ; ses autres profils utilisent la langue originale. Dans Sonarr, les scores favorisent le français sans l'imposer à eux seuls. Les exclusions dépendent du profil : notamment x265 en HD pénalisé dans HD-1080p. Les Remux 1080p sont autorisés dans ce profil Radarr, pas dans son équivalent Sonarr. [Politique détaillée](docs/QUALITY.md).

## VPN et options

- **Sans VPN** : votre IP publique est utilisée pour les connexions sortantes ; l'assistant demande votre accord explicite.
- **Avec VPN** : qBittorrent, Prowlarr, Radarr, Sonarr et Byparr (si activé) passent par Gluetun. Les lecteurs Jellyfin restent en accès direct.
- **ProtonVPN** : chemin prévu pour WireGuard ou OpenVPN ; voir [le guide VPN](docs/VPN.md) et ses limites de qualification.
- **Autres fournisseurs** : possibles via Gluetun, non qualifiés ; le port forwarding dépend du fournisseur et de l'abonnement.
- **QUI/cross-seed** : option qui configure aussi le proxy et les accès disque, pas seulement une interface supplémentaire.
- **Byparr** : seulement si certains indexeurs en ont besoin ; ne contourne pas une inscription, un abonnement ou une 2FA.
- **WUD** : surveillance uniquement, aucune mise à jour automatique des applications.

Pour modifier les options d'une installation existante :

```bash
python3 mediaflow options --enable qui
python3 mediaflow options --enable byparr --enable wud
python3 mediaflow options --disable qui
```

Une sauvegarde précède la reconfiguration. Les états des options désactivées restent conservés.

## Exploiter sans mauvaises surprises

```bash
python3 mediaflow doctor
python3 mediaflow stop
python3 mediaflow start
python3 mediaflow backup
```

Pour rechercher les nouvelles images, puis choisir de les installer :

```bash
python3 mediaflow update --check
python3 mediaflow update
```

La première commande consulte les registres sans télécharger les couches ni redémarrer les applications. La seconde présente les changements et demande confirmation avant téléchargement, sauvegarde et déploiement. Les digests retenus restent épinglés localement ; WUD n'est pas nécessaire. Une nouvelle image amont n'est pas une nouvelle version qualifiée de MediaFlow : consultez les changements majeurs des applications avant d'accepter.

- [Sauvegarde, restauration et mise à jour](docs/OPERATIONS.md)
- [Dépannage et configuration avancée](docs/TROUBLESHOOTING.md)
- [Sécurité et publication](SECURITY.md)

**Ne publiez jamais `.state/` ni vos sauvegardes.** Ils contiennent vos mots de passe, clés API et bases applicatives. Le `.gitignore` les exclut, mais ne protège pas une archive faite manuellement.

Les interfaces sont destinées au LAN. Ne redirigez pas leurs ports sur Internet ; l'accès distant sécurisé est un chantier distinct.

Licence MIT pour les outils de ce dépôt ; chaque application et chaque catalogue tiers conserve sa propre licence.
