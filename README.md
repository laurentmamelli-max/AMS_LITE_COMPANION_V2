# AMS Lite Companion V2

Application macOS communautaire pour suivre le filament restant sur une
**Bambu Lab A1 mini équipée d’un AMS Lite**.

Companion fonctionne avec la version officielle et signée de Bambu Studio. Il
ne modifie pas le slicer et n’envoie aucune commande d’impression : sa
passerelle récupère automatiquement le `.gcode.3mf` créé par Bambu Studio lors
de l’envoi, surveille localement l’état de l’imprimante et met à jour les
bobines lorsque l’impression se termine correctement.

> Projet indépendant et non officiel, sans affiliation avec Bambu Lab.

## Fonctionnement

```mermaid
flowchart LR
    A["Bambu Studio officiel"] -->|".gcode.3mf automatique"| B["AMS Lite Companion"]
    B -->|"surveillance MQTT locale"| C["A1 mini + AMS Lite"]
    C -->|"RUNNING → FINISH"| B
    B -->|"déduction par couleur"| D["Niveaux A1 à A4"]
```

L’AMS Lite ne pèse pas les bobines génériques. Companion utilise donc les
valeurs `used_g` calculées par le trancheur. Les niveaux restent des
estimations et peuvent être corrigés manuellement après une pesée.

## Points principaux

- application native macOS avec un Centre Vision indépendant ;
- captures caméra locales toutes les cinq couches, regroupées par impression ;
- cartographie des objets Bambu Studio, alertes Vision locales et exclusion
  unitaire disponible uniquement après un choix manuel, une confirmation et
  une connexion MQTT d’impression active ;
- popups locaux dédupliqués pour les alertes Vision confirmées, sans action
  automatique ni bouton de commande dans la notification ;
- journal durable des événements MQTT importants, conservé après redémarrage ;
- poste de supervision qui explique l’état de l’imprimante, Vision, MQTT,
  Gardien, AutoPilot et cartographie sans jamais commander l’imprimante ;
- rapports locaux redigés, archivés à la demande et après chaque impression
  suivie, avec historique Vision/Gardien par type de défaut ;
- lancement de Bambu Studio officiel sans erreur de signature ;
- suivi indépendant des emplacements A1 à A4 ;
- catalogue local de bobines avec poids conservé lors des échanges A1–A4 ;
- impressions monochromes et multicolores ;
- récupération automatique du fichier temporaire de Bambu Studio ;
- correspondance A1–A4 enregistrée et configurable pour l’armement automatique ;
- extraction multifilament depuis `Metadata/slice_info.config` ;
- connexion MQTT TLS directe sur le réseau local ;
- déduction uniquement après `RUNNING → FINISH` ;
- aucune déduction après annulation ou échec ;
- protection contre les doubles déductions ;
- conservation du travail actif après redémarrage ;
- arrêt automatique lorsque Bambu Studio est fermé ;
- aucun service permanent en arrière-plan ;
- aucune dépendance Python externe ; la calibration Vision automatique utilise
  uniquement un composant macOS inclus dans l’application.

## Compatibilité

- macOS 10.15 ou ultérieur sur Mac Intel ;
- macOS 11 ou ultérieur sur Apple Silicon ;
- Python 3 installé ;
- Bambu Studio officiel placé dans `/Applications` ;
- A1 mini et Mac connectés au même réseau local.

L’application distribuée est universelle : elle contient les architectures
`x86_64` et `arm64`.

## Installation rapide

1. Ouvrez la [dernière version stable](https://github.com/laurentmamelli-max/AMS_LITE_COMPANION_V2/releases/latest).
2. Téléchargez l’archive macOS de la dernière version.
3. Décompressez l’archive.
4. Glissez `AMS Lite Companion.app` dans `/Applications`.
5. Au premier lancement, faites un clic droit sur l’application puis
   **Ouvrir**.

L’application est signée de manière ad hoc par défaut, mais elle n’est pas
notariée par Apple dans cette distribution. La confirmation du premier
lancement est donc normale. Une construction de distribution peut utiliser une
identité Developer ID et un profil `notarytool` (`CODESIGN_IDENTITY` et
`NOTARY_PROFILE`) pour signer et notariser le paquet.

Si Python 3 n’est pas installé :

```bash
brew install python
```

## Première configuration

1. Lancez `AMS Lite Companion.app`.
2. Bambu Studio officiel et le panneau Companion s’ouvrent automatiquement.
3. Dans les paramètres réseau de l’A1 mini, relevez :
   - son adresse IP ;
   - son numéro de série ;
   - son code d’accès LAN.
4. Saisissez ces données dans Companion.
5. Donnez un nom et un poids initial à chaque bobine A1–A4.
6. Cliquez sur **Enregistrer et connecter**.
7. Dans **Passerelle Bambu Studio**, vérifiez la correspondance de secours :
   filament 1 vers A1, filament 2 vers A2, etc. Modifiez-la si votre projet
   utilise une autre disposition.

Sur certains firmwares, l’accès MQTT local nécessite l’activation du mode
développeur dans les paramètres réseau de l’imprimante.

## Utilisation pour une impression

1. Préparez et tranchez le plateau dans Bambu Studio.
2. Cliquez normalement sur **Imprimer le plateau**.
3. Vérifiez dans Companion que le travail passe à **Armé automatiquement** si
   Bambu Studio a transmis sa correspondance AMS.
4. Sinon, cliquez sur **Confirmer le travail détecté** : la correspondance
   enregistrée A1–A4 est alors utilisée explicitement, jamais sur la base d’un
   ancien fichier seul.

Aucun export ni import manuel n’est normalement nécessaire. L’import manuel
reste disponible en secours si une version future de Bambu Studio change son
dossier temporaire.

Companion attend une transition réelle de l’imprimante de `RUNNING` vers
`FINISH`. Il effectue alors une seule déduction et l’ajoute à l’historique.

## Impression multicolore

Chaque filament est comptabilisé séparément. Exemple :

| Filament tranché | Emplacement | Consommation | Avant | Après |
|---|---:|---:|---:|---:|
| PLA noir | A1 | 18,2 g | 1 000 g | 981,8 g |
| PLA blanc | A3 | 7,4 g | 800 g | 792,6 g |
| PLA rouge | A4 | 2,1 g | 500 g | 497,9 g |

Le firmware de certaines A1 mini ferme la connexion des clients tiers qui
tentent de s’abonner au canal MQTT des commandes. Pour préserver une connexion
stable, Companion emploie la correspondance enregistrée dans le tableau de
bord. Celle-ci doit correspondre aux emplacements réellement utilisés dans
l’AMS Lite. La consommation dépend des données du trancheur et peut inclure les
changements de couleur et les purges selon le projet.

## Menu macOS

L’icône Companion dans la barre des menus permet de :

- voir l’état de la connexion et de l’impression ;
- consulter les niveaux A1–A4 ;
- afficher ou masquer le panneau Companion ;
- activer ou désactiver son suivi de la fenêtre Bambu Studio ;
- ouvrir le tableau complet dans le navigateur si nécessaire ;
- ouvrir Bambu Studio ;
- redémarrer le moteur de suivi ;
- afficher le journal ;
- quitter complètement Companion.

Lorsque Bambu Studio est fermé, Companion se ferme automatiquement après deux
contrôles successifs, soit environ six secondes.

## Panneau intégré et Centre Vision

V2 affiche le tableau de bord dans une fenêtre macOS native à côté
de Bambu Studio. Le panneau présente d’abord les bobines, puis l’état de
l’imprimante, la passerelle automatique et l’historique. Il suit les
déplacements de Bambu Studio tant que l’option **Suivre la fenêtre Bambu
Studio** est cochée dans le menu.

Pour déplacer le panneau librement, décochez cette option. Sa fermeture masque
seulement l’interface : le suivi continue et le panneau peut être réaffiché
depuis l’icône de la barre des menus. Le Centre Vision s’ouvre depuis le bouton
du tableau de bord : il présente les captures dans une fenêtre séparée, les
range par impression terminée et permet de supprimer un groupe pour libérer
l’espace local.

### Calibration automatique de la projection

Le Centre Vision peut superposer les objets du G-code sur une capture sans
sélectionner les coins du plateau. Dans **Projection des objets cartographiés** :

1. téléchargez la planche Companion de 180 mm et imprimez-la à **100 %**, sans
   adaptation à la page ;
2. posez-la à plat sur le plateau vide, sans lancer d’impression ;
3. prenez ou ouvrez une capture où les quatre QR sont nets ;
4. cliquez **Détecter la planche automatiquement**.

La détection et le calcul de perspective restent sur le Mac. La planche ne
commande pas l’imprimante et peut être retirée après l’enregistrement de la
calibration. Le réglage manuel reste disponible si une capture ne permet pas
de lire les quatre QR.

## Catalogue de bobines

La version 1.4 conserve chaque bobine dans une base locale SQLite, séparée des
emplacements A1–A4. Ajoutez une bobine au **Catalogue de bobines**, puis
choisissez sa voie AMS. Lorsqu’une bobine rouge est retirée pour installer une
verte, le poids restant de la rouge est conservé. Il suffit de la remettre plus
tard dans une voie pour reprendre son suivi au même poids.

Dans le catalogue, modifiez la fiche et la position puis cliquez une seule fois
sur **Enregistrer**. Si une bobine déjà placée est envoyée vers une voie occupée,
les deux bobines échangent leurs positions. Si une bobine hors AMS est placée
dans une voie occupée, elle remplace l’occupante, qui reste conservée dans le
catalogue mais passe hors AMS. Choisir **Hors AMS** retire seulement la bobine
sélectionnée. Chaque mouvement est indiqué dans son historique.

Le nom est libre, mais Companion propose automatiquement un nom descriptif à
partir de la matière et de la couleur, par exemple **PLA bleu**. La **date
d’ajout** peut aussi être choisie ou corrigée afin que la première entrée de la
frise corresponde à la date réelle d’une bobine déjà en stock.

Le bouton **Supprimer** demande une seconde confirmation, puis retire
définitivement la fiche, sa voie AMS et l’historique propre à cette bobine. Une
bobine utilisée par une impression déjà en cours ne peut pas être supprimée.

Avec une bobine Bambu Lab reconnue par l’AMS Lite, Companion récupère aussi
l’identifiant RFID transmis par l’imprimante. La fiche est alors placée
automatiquement dans la bonne voie et sera retrouvée au même poids si cette
même bobine est remise plus tard. La colonne **RFID** du catalogue permet de
le vérifier. Les bobines sans tag (ou dont l’imprimante ne transmet pas
l’identifiant) restent gérées manuellement afin de ne jamais confondre deux
bobines de même couleur.

Le débit d’une impression est associé à la bobine présente au démarrage de
l’impression. Un échange effectué après `RUNNING` ne peut donc pas débiter la
nouvelle bobine par erreur.

## Données et confidentialité

L’interface web écoute uniquement sur `127.0.0.1:8766`. Les données restent
sur le Mac dans :

```text
~/Library/Application Support/AMS Lite Companion V2/state.json
```

Le catalogue est stocké à côté dans :

```text
~/Library/Application Support/AMS Lite Companion V2/inventory.sqlite3
```

Le journal de diagnostic se trouve dans :

```text
~/Library/Application Support/AMS Lite Companion V2/companion.log
```

Le dossier, `state.json`, la base SQLite et le journal sont créés avec des
droits réservés à votre compte. `state.json` contient le code d’accès LAN afin
de permettre la reconnexion. Ne publiez jamais ce fichier et ne le joignez pas
à une issue GitHub. Si ce fichier devient illisible, Companion le sauvegarde
automatiquement sous le nom `state.corrompu-…json` au lieu de l’écraser.

Une mise à jour de l’application ne supprime ni les niveaux ni l’historique.

## Dépannage

### L’application ne s’ouvre pas

Effectuez un clic droit sur `AMS Lite Companion.app`, choisissez **Ouvrir**,
puis confirmez. Vérifiez également que Python est disponible :

```bash
python3 --version
```

### Bambu Studio est introuvable

Installez la version officielle dans l’un des emplacements suivants :

```text
/Applications/BambuStudio.app
/Applications/Bambu Studio.app
```

### L’imprimante reste déconnectée

- vérifiez l’adresse IP ;
- vérifiez le numéro de série et le code LAN ;
- confirmez que le Mac et l’imprimante sont sur le même réseau ;
- contrôlez le mode développeur de l’imprimante ;
- ouvrez le journal depuis le menu Companion.

L’erreur `nodename nor servname provided, or not known` correspond généralement
à une adresse IP vide ou incorrecte.

### Le port 8765 est déjà utilisé

Une ancienne instance est probablement encore active. Ouvrez
<http://127.0.0.1:8765>, cliquez sur **Arrêter Companion**, puis relancez
l’application.

### Aucun poids n’est déduit

Vérifiez l’état de la carte **Passerelle Bambu Studio**, la correspondance de
secours A1–A4 et que le travail était indiqué comme **Armé** avant le démarrage.
Le journal doit contenir `archive détectée`, puis `travail armé
automatiquement`. En l’absence de détection, utilisez temporairement l’import
manuel et joignez le journal à un rapport de problème sans publier
`state.json`.

## Construire l’application

Les outils Apple et Python 3 sont nécessaires :

```bash
xcode-select --install
brew install python
git clone https://github.com/laurentmamelli-max/AMS_LITE_COMPANION_V2.git
cd AMS_LITE_COMPANION_V2
chmod +x Construire_Application_macOS.command
./Construire_Application_macOS.command
```

Le script :

- compile les variantes Apple Silicon et Intel ;
- assemble un exécutable universel ;
- crée le bundle `.app` ;
- applique une signature ad hoc ;
- vérifie la signature ;
- produit l’archive dans `dist/`.

## Tests

```bash
python3 -m unittest -v test_companion.py
python3 -m py_compile ams_companion.py test_companion.py
```

GitHub Actions teste le moteur sur plusieurs versions de Python et construit
l’application sur un runner macOS avant publication.

## Limites

- Le poids est estimé par le trancheur et non mesuré physiquement.
- Si la commande AMS locale n’est pas retransmise au Companion, la
  correspondance de secours doit refléter la disposition A1–A4 du projet.
- Un changement futur du dossier temporaire de Bambu Studio peut nécessiter
  une mise à jour de la passerelle ; l’import manuel reste disponible.
- Les impressions partielles annulées ne sont pas débitées automatiquement.
- Une pesée occasionnelle reste recommandée pour corriger la dérive.
- Les autres modèles d’imprimantes Bambu ne sont pas encore validés.

## Licence

AMS Lite Companion est distribué sous licence [MIT](LICENSE).

Bambu Studio, Bambu Lab, A1 mini et AMS Lite sont des marques de leurs
propriétaires respectifs.
