# AMS Lite Companion V2

Branche de développement : `companion-v2`.

V2 démarre sur la V1.5 stable, mais ses évolutions restent isolées de la
branche `main` jusqu'à leur validation. La première livraison stable est
`2.0.1`. La livraison locale a atteint `3.2.1` ; l’architecture V3 est
documentée dans `V3_ARCHITECTURE.md`.

## Isolation avec V1

- Dépôt de travail : `AMS-Lite_Companion_V2`, distinct du dossier V1.
- Application : `AMS Lite Companion V2.app`, avec son propre identifiant macOS.
- État, catalogue SQLite et journaux : `~/Library/Application Support/AMS Lite Companion V2`.
- Interface locale : `127.0.0.1:8766` (V1 conserve le port `8765`).

Les deux versions peuvent donc être construites et lancées sans partager leurs
données. Elles ne doivent toutefois pas suivre une même imprimante en même
temps, afin d'éviter deux suivis du même travail.

## Principes conservés

- Aucune commande envoyée à l'imprimante : Companion observe et comptabilise.
- Les données restent locales sur le Mac.
- Une déduction est toujours liée à une transition réelle `RUNNING` → `FINISH`
  et reste idempotente.
- Les migrations de données sont réversibles et sauvegardent l'état existant.

## Premier périmètre V2

1. **Gardien de plateau** — surveiller l'impression en direct afin de détecter
   un objet qui se décolle, se déforme ou échoue. Si Bambu Studio ou
   l'imprimante expose une commande fiable pour cela, V2 devra annuler
   uniquement cet objet et laisser les autres objets du plateau continuer.
   La première version proposera toujours l'action à l'utilisateur : aucune
   annulation ne sera déclenchée silencieusement.
2. **Centre de suivi d'impression** — une fiche de travail unique qui expose
   clairement le fichier, le plateau, les bobines verrouillées au démarrage et
   l'état de la déduction.
3. **File d'événements durable** — journaliser les événements MQTT importants
   avant de les traiter, afin de rendre une reprise après coupure réseau
   vérifiable et récupérable.
4. **Décisions AMS explicites** — remplacer les messages transitoires par une
   vue qui explique la correspondance reçue, le choix appliqué et son impact.
5. **Catalogue orienté atelier** — recherche par marque/matière/couleur,
   alertes de stock et préparation des bobines avant un projet.

## Prérequis du gardien de plateau

1. Identifier la source de surveillance (caméra de l'imprimante ou caméra
   externe) et définir les défauts réellement détectables.
2. Vérifier, sur une impression de test, que la commande Bambu peut annuler un
   objet précisément identifié sans annuler le plateau entier.
3. Associer sans ambiguïté un objet détecté à son objet dans le fichier
   d'impression.
4. Conserver une trace de la détection, de la décision et de la commande
   envoyée ; une action ne doit jamais être répétée après un redémarrage.

## Jalons

| Jalon | Résultat attendu | État |
|---|---|---|
| V2-0 | Branche, version de développement et feuille de route | Fait |
| V2-1 | Vision : preuves, catégories de défaut et alertes | Fait, modèle local à brancher |
| V2-2 | Cartographie G-code d’objets, zones XY et segments Bambu Studio | Fait, validé sur un 3MF Bambu réel |
| V2-3 | Exclusion unitaire préparée, canonique et journalisée | Fait, publication MQTT bloquée |
| V2-4 | Poste de supervision, santé expliquée et signaux de fiabilité | Fait |
| V2-5 | Historique Vision/Gardien et rapports locaux archivés | Fait |
| V3-0 | Alertes locales et popups, exclusion uniquement manuelle | Fait |

## Règle de livraison

Chaque lot V2 doit inclure ses tests et une migration de données testée sur une
copie de catalogue. `main` reste la branche de production tant qu'une version
V2 n'est pas explicitement validée.
