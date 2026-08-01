# Changelog

## 3.1.1 — 2026-08-01

- La calibration Vision affiche désormais les repères jaunes de chaque coin
  sélectionné, avec annulation et redémarrage immédiats.
- Lorsqu’un coin du plateau est réellement hors champ de la caméra, il peut
  être signalé : les trois coins visibles permettent une estimation locale du
  quatrième. La projection est alors explicitement indiquée comme indicative.

## 3.0.2 — 2026-07-28

- Chaque nouvelle impression remet à zéro le curseur de couches Vision. Une
  capture à la couche 5 n’est donc plus bloquée par la dernière couche de
  l’impression précédente.

## 3.0.1 — 2026-07-28

- V3 adopte un fonctionnement **alertes uniquement** : une détection Vision
  confirmée apparaît dans le tableau de bord et dans une notification macOS
  locale, dédupliquée pendant la session. Aucun de ces mécanismes ne prépare
  ou n’envoie une commande à l’imprimante.
- L’exclusion d’un objet est une décision manuelle explicite. Le tableau de
  bord permet de l’envoyer une seule fois après confirmation, uniquement avec
  un MQTT connecté et une impression active ; les demandes expirées ou perdues
  ne sont jamais rejouées après reconnexion.
- Les préparations locales V2.3 existantes sont migrées dans cet historique
  manuel, sans jamais être exécutées pendant la mise à niveau.

## 3.0.0 — 2026-07-28

- Première livraison V3 : supervision locale, alertes Vision et notifications
  macOS sans aucune action automatique.

## 2.5.0 — 2026-07-28

- Historique Vision/Gardien enrichi par type de défaut et état de décision,
  sans confondre une observation avec une action sur l’imprimante.
- Les instantanés de supervision peuvent être archivés à la demande et un
  rapport redigé est créé automatiquement à la fin d’une impression suivie.
  Chaque rapport est durable, téléchargeable individuellement, borné à 500
  entrées et ne contient ni code LAN, ni adresse IP, ni numéro de série.
- Une même fin d’impression ne peut créer qu’un rapport automatique grâce à
  une clé idempotente locale.

## 2.4.0 — 2026-07-28

- Nouveau poste de supervision : santé de l’imprimante, Vision, fraîcheur et
  erreurs MQTT, Gardien, AutoPilot et cartographie apparaissent dans une seule
  vue avec un niveau et une explication explicite.
- Les indicateurs sont calculés depuis les faits persistés ; une caméra sans
  empreinte TLS, un MQTT silencieux durant une impression ou une alerte en
  attente sont signalés sans jamais provoquer de commande matérielle.
- Le rapport JSON inclut cette synthèse opérationnelle, avec les mêmes règles
  de redaction des données sensibles.

## 2.3.0 — 2026-07-28

- L’identité d’exclusion est désormais l’identifiant canonique `identify_id`
  de `Metadata/slice_info.config`, recoupé avec les segments du G-code. Un
  objet absent, déjà ignoré ou sans travail actif est bloqué par préconditions.
- Le Gardien permet de préparer une exclusion unitaire : la requête
  `skip_objects` est construite, validée puis journalisée dans une base SQLite
  dédiée, de façon idempotente. Cette étape ne possède aucun transport MQTT et
  n’envoie donc aucune commande à l’imprimante.
- Le tableau de bord affiche la préparation et son statut, en distinguant les
  propositions encore bloquées des commandes locales journalisées.

## 2.2.0 — 2026-07-28

- Cartographie G-code étendue au format réel de Bambu Studio : les marqueurs
  `start/stop printing object` sont agrégés sur toutes les couches d’un même
  objet, avec enveloppe XY et plages de lignes de l’outil de découpe.
- La cartographie est affichée directement dans le tableau de bord ; le
  gardien et AutoPilot utilisent désormais les identifiants objets Bambu réels.
- Validation effectuée sur le 3MF Bambu Studio local : sept objets ont été
  retrouvés, chacun avec une zone XY et des segments multi-couches.

## 2.1.0 — 2026-07-27

- Ajout d’un journal durable local des événements MQTT d’impression. Chaque
  rapport utile est écrit avant son traitement, puis marqué traité ou en échec
  afin de rendre une coupure réseau ou un redémarrage vérifiable.
- Le journal ne stocke ni le code LAN ni le contenu brut des messages MQTT ;
  il conserve uniquement l’état, le travail, la couche, la progression et le
  résultat du traitement.
- Le tableau de bord affiche les derniers événements de fiabilité, et l’API
  locale expose le journal complet protégé par le même jeton de session.
- Un rapport de supervision JSON peut être téléchargé depuis le tableau de
  bord. Il rassemble l’état d’impression, Vision, Gardien, AutoPilot et le
  journal de fiabilité sans jamais inclure d’identifiant d’imprimante, chemin,
  code LAN ou message MQTT brut.
- Centre Vision indique désormais le nombre d’images indexées et leur espace
  disque réel. Les fichiers non référencés ne sont pas comptés.
- Première cartographie G-code : les objets explicitement balisés par le
  trancheur sont associés à leurs plages de lignes et zones XY. Le Gardien
  refuse une observation qui cible un objet absent de cette carte.
- AutoPilot fournit désormais des plans d’exclusion simulés et auditables. Une
  action physique reste volontairement bloquée tant que le protocole Bambu
  n’est pas documenté et validé.
- Les alertes Vision distinguent spaghetti, décollement, warping et extrusion
  anormale : les preuves de catégories différentes ne sont jamais mélangées.
- Documentation V2 corrigée : port local, répertoire de données et lien de
  release correspondent désormais à l’application V2.

## 1.5.0 — 2026-07-27

- La passerelle arme automatiquement lorsque Bambu Studio et la correspondance
  AMS enregistrée sont identiques. Une confirmation avec le détail du changement
  est désormais demandée uniquement si Bambu annonce une autre voie AMS. La
  fenêtre native permet alors de choisir Bambu Studio, la correspondance
  enregistrée, ou de décider plus tard.
- Lorsque l’impression démarre, l’état de la passerelle devient explicitement
  « suivi filament actif » : une ancienne confirmation ne reste plus affichée.
- La sélection de texte et de champ met temporairement le rafraîchissement du
  tableau en pause, afin de permettre le copier/coller normalement.
- Les tests écrivent désormais dans un journal temporaire : les scénarios
  simulés ne polluent plus `companion.log`. Les déconnexions MQTT indiquent la
  tentative et le délai de reconnexion pour distinguer une veille réseau d’une
  indisponibilité persistante.
- Nouveau gestionnaire de catalogue conçu pour des centaines de bobines :
  recherche instantanée, filtres combinables, tri et pagination par 50 lignes.
- Fiches de bobine complètes : emplacement physique, seuil de réapprovisionnement,
  coût, notes, position AMS et historique de consommation.
- Tableau de bord de stock, alertes de niveau bas, sélection et actions groupées
  (déplacement, seuil, archivage non destructif) ainsi qu’export CSV.
- Migration SQLite automatique et rétrocompatible : aucune bobine ni historique
  existant n’est supprimé lors de la mise à jour.

## 1.4.13 — 2026-07-27

- Correction de la règle CSS qui empêchait le reste de la feuille de style de
  s’appliquer dans le panneau natif : les cartes, colonnes et espacements sont
  de nouveau rendus normalement.
- Retrait de l’import manuel de secours du panneau. L’application s’appuie
  désormais uniquement sur la passerelle automatique Bambu Studio.

## 1.4.12 — 2026-07-26

- Correction du rendu du tableau graphique du catalogue.
- Les métadonnées du dernier fichier Bambu Studio sont conservées localement
  pendant 24 heures : une impression préparée longtemps à l’avance ou une
  suppression du fichier temporaire par Bambu Studio n’empêche plus l’armement
  automatique après relance de Companion.

## 1.4.11 — 2026-07-26

- Le catalogue affiche désormais une synthèse de toutes les bobines (niveau,
  emplacement, dernière utilisation, nombre d’impressions), une courbe de
  poids par bobine avec détail au survol et la frise chronologique existante.

## 1.4.10 — 2026-07-26

- La passerelle arme automatiquement le fichier Bambu Studio dès sa détection,
  avec la correspondance A1–A4 enregistrée si Bambu ne fournit pas encore la
  sienne. Un fichier récent est aussi restauré automatiquement après une
  relance de Companion.
- L’état `FINISH` publié au démarrage par l’imprimante ne crée plus une fausse
  entrée d’historique ; un suivi incomplet n’est enregistré qu’après un
  `RUNNING` réellement observé.

## 1.4.9 — 2026-07-26

- La fenêtre de préparation d’un fichier Bambu Studio passe à deux heures : un
  travail préparé puis lancé plus tard reste associable. Les commandes MQTT
  restent limitées à 90 secondes afin de ne jamais réutiliser une ancienne
  commande avec un nouveau fichier.
- Une impression terminée sans fichier 3MF associé reste désormais visible dans
  l’historique, clairement marquée « sans décompte », au lieu de disparaître.

## 1.4.8 — 2026-07-26

- Après l’expiration de l’archive temporaire, la passerelle revient clairement
  en attente du prochain travail Bambu Studio, au lieu d’afficher un faux état
  bloqué de confirmation requise. Une nouvelle commande ou archive relance le
  cycle normalement.

## 1.4.7 — 2026-07-26

- Les impressions annulées ou en échec avant l’état `RUNNING` sont maintenant
  conservées dans l’historique, avec la mention qu’aucun filament n’a été
  débité. Les répétitions MQTT du même événement ne créent pas de doublon.

## 1.4.6 — 2026-07-26

- Correction d’un plantage macOS lors de l’ouverture du catalogue de bobines
  depuis le panneau Companion. La fenêtre est maintenant créée hors du callback
  WebKit et reste possédée explicitement jusqu’à sa fermeture.

## 1.4.5 — 2026-07-26

- Le catalogue ne se rafraîchit plus pendant l’édition : le choix d’une voie
  A1–A4 reste immédiatement visible et le bouton Enregistrer reste cliquable.
- Le changement de voie est marqué instantanément, sans dépendre de la
  propagation d’événements du tableau macOS/WebKit.

## 1.4.4 — 2026-07-26

- Stabilisation MQTT : fermeture systématique des sockets, reconnexion propre et
  isolation d’un événement imprévu sans perdre toute la connexion.
- Les confirmations JavaScript de l’interface sont prises en charge nativement
  dans la fenêtre macOS ; l’état du moteur est vérifié avec son jeton réel.
- La vue A1–A4 est synchronisée et enregistrée dès le démarrage.
- Supprimer une bobine retire aussi ses lignes de l’historique général des
  impressions, y compris dans une impression multibobine.

## 1.4.3 — 2026-07-26

- Suppression définitive d’une bobine et de tout son historique, confirmée par
  un second clic fiable dans la fenêtre macOS.
- Import unique dans le catalogue de l’historique des impressions antérieur à
  la migration du 26 juillet, en conservant les dates d’origine.
- Les libellés RFID techniques (par exemple `A01-W2`) sont remplacés par des
  noms lisibles tels que « PLA blanc », sans écraser un nom personnalisé.
- Le lanceur conserve son jeton de session entre relances, signale une autre
  instance incompatible et écrit ses erreurs moteur dans `launcher.log`.

## 1.4.2 — 2026-07-26

- Les déplacements de bobines sont désormais atomiques et explicites : échange
  des deux voies, remplacement avec sortie hors AMS, ou retrait idempotent.
- Le clic sur une ligne du catalogue ouvre son historique ; les champs restent
  éditables sans déclencher la frise.
- Ajout de l’archivage sécurisé d’une bobine, qui libère sa voie tout en
  conservant l’audit, avec protection pendant une impression active.
- Nom descriptif proposé automatiquement à partir de la matière et de la
  couleur (par exemple « PLA bleu ») et date d’ajout rétrodatable dans la
  première entrée de l’historique.

## 1.4.1 — 2026-07-26

- Décompte multi-bobines rendu atomique et idempotent dans SQLite : un arrêt
  entre le débit et la sauvegarde ne peut plus débiter une même impression deux fois.
- Sauvegarde automatique de `state.json` corrompu avant récupération ; données,
  journal et répertoire applicatif protégés avec des droits réservés à l’utilisateur.
- API locale protégée par un jeton aléatoire de session, contrôle strict de
  l’hôte/origine et validation des types de requêtes.
- Limites ajoutées aux imports 3MF/ZIP pour refuser les archives anormalement
  volumineuses ou fortement compressées.
- Les fichiers Bambu Studio récupérés avec la correspondance enregistrée
  nécessitent désormais une confirmation explicite ; la correspondance reçue
  dans une commande Bambu récente reste armée automatiquement.
- Certificat MQTT local épinglé lors de la première connexion et refusé s’il change.
- Archive macOS sans métadonnées Finder, artefact CI corrigé et construction
  prête pour une signature Developer ID et une notarisation optionnelles.

## 1.4.0 — 2026-07-26

- Ajout du catalogue local SQLite de toutes les bobines.
- Les voies A1–A4 deviennent des emplacements temporaires : retirer puis remettre une bobine conserve son poids estimé.
- Migration automatique des quatre bobines existantes depuis `state.json` au premier démarrage.
- Le débit est lié à l’identité de la bobine présente à `RUNNING`, même après un échange ultérieur.
- Ajout des contrôles de création, placement et retrait depuis le tableau de bord complet.
- Synchronisation RFID automatique des bobines Bambu reconnues par l’AMS Lite,
  avec réassociation de la même fiche et de son poids lors d’un retour dans l’AMS.
- Catalogue déplacé dans une fenêtre macOS indépendante, sous forme de tableau éditable.

## 1.3.0 — 2026-07-19

- Panneau macOS natif lié à Bambu Studio officiel, sans modifier sa signature.
- Récupération automatique du paquet d’impression `.gcode.3mf` sous `Metadata`.
- Connexion MQTT locale stable sur le canal `report` des A1 mini et AMS Lite.
- Décompte monochrome et multicolore avec correspondance A1–A4 enregistrée.
- Déduction unique après la transition réelle `RUNNING → FINISH`.
- Aucune déduction après annulation, échec ou remplacement d’un ancien travail.
- Protection contre les sauvegardes de projet, réarmements et doubles déductions.
- Validation sur plusieurs impressions réelles, dont une impression bicolore.

## 1.3.0-beta.3 — 2026-07-19

- Analyse du journal réel d’une impression complète avec la bêta 2.
- Surveillance limitée aux paquets d’impression situés dans `Metadata`.
- Exclusion des sauvegardes de projet `.3mf` créées à la racine par Bambu Studio.
- Consommation définitive de l’import automatique après `FINISH`, annulation ou échec.
- Suppression au démarrage des anciens armements automatiques devenus périmés.
- Protection testée contre le réarmement et une future déduction parasite.

## 1.3.0-beta.2 — 2026-07-19

- Correction des déconnexions MQTT répétées sur A1 mini et AMS Lite.
- Abonnement limité au canal `report` accepté par le firmware ; le canal `request` reste réservé à l’envoi de `pushall`.
- Détection d’un nouvel identifiant de tâche après une coupure réseau.
- Abandon de l’ancien travail bloqué sans aucune déduction avant d’armer le nouveau.
- Correspondance A1–A4 enregistrée explicitement utilisée par la passerelle automatique.

## 1.3.0-beta.1 — 2026-07-19

- Ajout d’un panneau macOS natif intégré à côté de Bambu Studio officiel.
- Affichage du tableau Companion dans WebKit, sans ouverture obligatoire du navigateur.
- Suivi automatique de la position de la fenêtre Bambu Studio, désactivable depuis le menu.
- Accès séparé au tableau complet dans le navigateur pour les fonctions de secours.
- Navigation du panneau limitée au serveur local Companion.
- Conservation de la signature et de toutes les fonctions d’impression de Bambu Studio officiel.

## 1.2.0 — 2026-07-18

- Ajout de la passerelle automatique avec Bambu Studio officiel.
- Récupération du `.gcode.3mf` temporaire créé lors de l’envoi de l’impression.
- Détection de la correspondance AMS A1–A4 depuis la commande locale lorsque disponible.
- Correspondance enregistrée configurable en solution de repli.
- Attente d’un fichier ZIP stable et priorité stricte au projet le plus récent.
- Conservation de l’import manuel comme solution de secours.

## 1.1.0 — 2026-07-18

- Ajout d’une véritable application dans la barre des menus macOS.
- Lancement automatique de Bambu Studio officiel.
- Affichage direct des niveaux A1–A4 dans le menu macOS.
- Ouverture du tableau, du journal et redémarrage du moteur depuis l’icône.
- Arrêt automatique de Companion lorsque Bambu Studio est fermé.
- Construction locale et signature ad hoc automatisées.

## 1.0.0 — 2026-07-17

- Première version publique.
- Suivi persistant des quatre emplacements AMS Lite.
- Extraction de la consommation depuis les fichiers `.gcode.3mf`.
- Surveillance MQTT locale de `RUNNING → FINISH`.
- Protection contre les doubles déductions et les valeurs négatives.
- Conservation du travail actif après redémarrage.
- Interface web locale et bouton d’arrêt propre.
- Lanceurs macOS séparé et combiné avec Bambu Studio officiel.
