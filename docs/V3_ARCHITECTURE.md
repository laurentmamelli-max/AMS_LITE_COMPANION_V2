# Architecture V3 — supervision sûre d’impression

V3 est composée de cinq briques séparées, communiquant uniquement par des
structures de données locales et persistées. Aucune brique Vision, G-Code ou
Dashboard n’envoie directement une commande à l’imprimante.

```mermaid
flowchart LR
  C["Core\nétat, MQTT, journal durable"] --> V["Vision\npreuves et détections"]
  C --> G["G-Code\nobjets, lignes, zones XY"]
  V --> D["Dashboard\nalertes et supervision"]
  G --> D
  V --> A["AutoPilot\nalertes + exclusion manuelle"]
  G --> A
  A --> D
```

## Core

- suit l’imprimante localement via MQTT TLS épinglé ;
- enregistre les rapports d’impression importants dans `events.sqlite3` avant
  leur traitement ;
- conserve l’inventaire, les déductions idempotentes et les captures sans les
  exposer au réseau ;
- n’accorde aucun privilège de commande à ses sous-modules.

## Vision — V2.1

Les détecteurs doivent soumettre des observations normalisées : type de défaut,
confiance, empreinte de l’image, objet ciblé et source. Le Gardien exige
plusieurs images distinctes avant de créer une alerte. Les catégories prévues
sont `spaghetti`, `detachment`, `warping` et `extrusion_anomaly`.

Un modèle local peut être ajouté comme adaptateur, mais son résultat reste une
observation : il ne peut jamais déclencher une commande par lui-même.

## G-Code — V2.2

`gcode_mapper.py` extrait uniquement les objets déclarés explicitement par le
G-code (balises `OBJECT`/`PRINTING_OBJECT` et fin d’objet). Chaque objet porte
sa plage de lignes et son enveloppe XY. Sans balise fiable, l’état reste
`unavailable` : aucune association inventée n’est autorisée.

## AutoPilot — V3.0

V3 est en mode `alert_only_with_manual_exclusion`. Une détection confirmée ne
fait qu’ouvrir une alerte locale, dans le tableau de bord et dans la fenêtre
macOS. Aucun popup, minuteur, détecteur ou traitement en arrière-plan ne peut
préparer ni transmettre une action.

Après vérification visuelle, l’utilisateur peut choisir **Exclure réellement
cet objet** pour un seul objet. Cette option recoupe l’identité canonique de
`slice_info.config`, exige une impression MQTT connectée et une seconde
confirmation, puis publie une seule demande locale. Elle n’est jamais
déclenchée par une alerte, un popup ou une tâche de fond, n’est jamais rejouée
après reconnexion et chaque tentative est journalisée.

## Dashboard et rapports — V2.4/V2.5

Le tableau de bord agrège l’état MQTT, la cartographie, les alertes Vision,
les alertes AutoPilot, l’espace de stockage Vision et le journal durable. Les
rapports doivent rester exportables localement et ne jamais inclure code LAN,
jetons ou données non nécessaires.

V2.4 transforme ces faits en signaux expliqués (stable, information, à
vérifier, intervention ou hors ligne), notamment la fraîcheur MQTT pendant une
impression. V2.5 archive des instantanés redigés dans `reports.sqlite3` à la
demande et à chaque fin d’impression idempotente. L’historique Vision indique
les catégories et décisions du Gardien ; il ne prétend jamais qu’une
observation est une commande appliquée.
