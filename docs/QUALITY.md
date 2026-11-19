# Profils, formats vidéo et audio

La configuration distribuée reprend une politique complète issue de [TRaSH Guides](https://trash-guides.info/), avec ses définitions et scores figés dans le dépôt. Elle ne choisit pas automatiquement une nouvelle politique au gré des changements du catalogue amont.

## Quel profil utiliser ?

**Seerr utilise HD-1080p pour les films et les séries.** Les cinq autres profils restent disponibles dans Radarr/Sonarr pour vos choix explicites. Aucune recherche globale n'est lancée à l'installation.

| Profil | Films : Radarr | Séries : Sonarr |
|---|---|---|
| Any | Toutes les qualités sauf Unknown et Raw-HD, jusqu'au Blu-ray disque | SD à 1080p, sans Remux ni Raw-HD |
| SD | Qualités basses et SD, jusqu'au Blu-ray 576p | SD jusqu'au Blu-ray 576p |
| HD-720p | HDTV, WEB et Blu-ray 720p | Identique |
| **HD-1080p** | HDTV, WEB, Blu-ray et **Remux 1080p** | HDTV, WEB et Blu-ray 1080p, **sans Remux** |
| Ultra-HD | HDTV, WEB, Blu-ray et Remux 2160p | HDTV, WEB et Blu-ray 2160p, sans Remux |
| HD - 720p/1080p | Qualités HD précédentes, Remux 1080p compris | Qualités HD précédentes, sans Remux |

WEB-DL et WEBRip sont regroupés à chaque résolution. L'ordre et les seuils de qualité sont appliqués séparément pour chaque application. Les remplacements automatiques pour améliorer une qualité sont **désactivés dans tous les profils**.

## Langues : deux réglages différents

- **Radarr HD-1080p exige la langue French**, telle qu'identifiée par Radarr. Les cinq autres profils Radarr utilisent **Original**, pas Any.
- Sonarr n'a pas ce sélecteur de langue par profil. Ses formats personnalisés favorisent certaines versions françaises.
- Le seuil de score des formats personnalisés est `0`. L'absence d'un marqueur VFF/VFQ/etc. n'est donc pas, à elle seule, un motif de rejet. Cela ne désactive pas le filtre de langue distinct de Radarr.

| Format français | HD-1080p Radarr et Sonarr | Ultra-HD Radarr |
|---|---:|---:|
| VFF / TRUEFRENCH | 1000 | 1000 |
| VFQ | 800 | 0 |
| VFI | 700 | 0 |
| VOF | 600 | 0 |
| VF2 | 500 | 0 |

Les autres profils ont des scores nuls pour ces formats. Les scores ne sont pas des règles universelles appliquées à toutes les qualités.

## Ce qui est rejeté dans HD-1080p

**Radarr** applique `−10000` à : 3D, BR-DISK, LQ, Upscaled, Extras, x265 (HD), x265 (no HDR/DV), Bad Dual Groups, No-RlsGroup, Obfuscated et Retags.

**Sonarr** applique `−10000` à : BR-DISK, LQ, x265 (HD) et Extras.

En particulier, **x265/HEVC en HD est pénalisé**. Ce n'est pas une interdiction générale du HEVC en 4K : les conditions de résolution et les scores par profil sont distincts. Les expressions exactes des formats sont consultables dans Settings → Custom Formats.

Radarr HD-1080p donne aussi `+100` à **FR Remux Tier 02**. FanSUB reste à `0`.

Il n'y a **pas de préférence ajoutée pour Atmos, DTS, AAC ou AC3**. Ne confondez pas les marqueurs de version française, le codec vidéo et le codec audio.

## Formats neutres

Les formats de services de streaming sont présents avec un score nul :

- Radarr : AMZN, ATVP, DSNP, HMAX, Hulu, NF, PCOK, PMTP.
- Sonarr : AMZN, ATVP, DCU, HMAX, HULU, NLZ.

Le catalogue contient au total **26 formats Radarr et 15 Sonarr**. Ces noms décrivent les releases ; ce ne sont pas des comptes ni des connexions aux services de streaming.

## Tailles et renommage

Les limites de taille sont appliquées par qualité, pas comme une limite globale par fichier. Les qualités HD ont notamment des minimums mais pas de maximum imposé.

| Qualité | Minimum Radarr, Mo/min | Minimum Sonarr, Mo/min |
|---|---:|---:|
| Blu-ray 720p | 25,7 | 17,1 |
| Blu-ray 1080p | 50,8 | 50,4 |
| Remux 1080p | 102 | 69,1 |
| Remux 2160p | 187,4 | 187,4 |

Les films sont renommés `{Movie Title} ({Release Year}) {Quality Full}` dans un dossier `{Movie Title} ({Release Year})`.

Les épisodes utilisent `{Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}` ; les séries quotidiennes utilisent la date. Dossiers de saisons : `Season {season}`, épisodes spéciaux : `Specials`. Les autres réglages de nommage sont également provisionnés.

## Rôle de Recyclarr et personnalisation

Le bootstrap configure par API les profils, langues, groupes de qualités, tailles et renommage. Recyclarr synchronise quotidiennement **tous les formats et leurs scores dans les six profils** à partir du catalogue local généré sous `.state/apps/recyclarr/policy/`.

Les [ressources locales Recyclarr](https://recyclarr.dev/reference/settings/resource-providers/) empêchent une synchronisation quotidienne de remplacer ces définitions par des variantes amont. Recyclarr ne modifie pas les tailles : elles restent celles du catalogue appliquées par le bootstrap. Aucun ancien identifiant numérique de base n'est réutilisé.

- Les formats et profils que vous ajoutez avec **d'autres noms** sont conservés, ainsi que leurs scores sur les profils gérés.
- Modifier un format ou un score géré dans l'interface peut être annulé par la prochaine synchronisation Recyclarr.
- Relancer `install` réapplique aussi les profils, langues, tailles et renommage gérés.
- Pour adapter durablement la politique, modifiez `mediaflow_lib/presets/quality_data.json` dans votre copie, sauvegardez, puis relancez `python3 mediaflow install`. Conservez cette personnalisation lors d'une mise à jour des sources.
- Les sources ne contiennent ni compte de tracker, ni bibliothèque, ni identifiant de votre serveur.
