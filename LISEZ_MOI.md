# AMS Lite Companion pour macOS

Ce compagnon laisse **Bambu Studio officiel** envoyer les impressions. Il suit
localement l’état de l’A1 mini et déduit la consommation estimée du fichier
tranché lorsque l’imprimante passe réellement de `RUNNING` à `FINISH`.

## Prérequis

- Mac et imprimante sur le même réseau local ;
- Python 3 (`python3 --version`) ;
- adresse IP, numéro de série et code d’accès LAN de l’imprimante ;
- Bambu Studio officiel installé dans `/Applications`.

Le compagnon n’utilise ni le cloud Bambu ni son module propriétaire. Aucune
dépendance Python n’est nécessaire.

## Installation de l’application macOS

1. Construisez l’application en double-cliquant sur
   `Construire_Application_macOS.command`.
2. Ouvrez le dossier `dist` créé automatiquement.
3. Glissez `AMS Lite Companion.app` dans `/Applications`.
4. Au premier lancement, faites un clic droit sur l’application puis
   **Ouvrir**.

Une icône apparaît dans la barre des menus, Bambu Studio officiel démarre et un
panneau natif Companion vient se placer à côté de sa fenêtre. Le menu affiche
directement les niveaux A1–A4 et permet de masquer, réafficher ou décrocher ce
panneau. Lorsque Bambu Studio est fermé, Companion s’arrête automatiquement et
ne continue pas en arrière-plan.

Les données déjà créées sont conservées : l’application utilise toujours le
même fichier `state.json`.

## Démarrage historique par Terminal

Dans Terminal :

```bash
cd ~/Downloads/AMS_Lite_Companion
chmod +x Lancer_AMS_Lite_Companion.command ams_companion.py
./Lancer_AMS_Lite_Companion.command
```

L’interface s’ouvre sur <http://127.0.0.1:8765>.

Pour démarrer simultanément Bambu Studio officiel et le compagnon :

```bash
chmod +x Lancer_BambuStudio_et_Companion.command
./Lancer_BambuStudio_et_Companion.command
```

Le lanceur combiné garde une fenêtre Terminal ouverte. Appuyez sur `Ctrl+C`
dans cette fenêtre pour arrêter proprement Companion ; aucun processus ne reste
ensuite en arrière-plan.

## Première configuration

1. Sur l’A1 mini, affichez le code d’accès LAN dans les paramètres réseau.
2. Saisissez l’IP, le numéro de série et ce code dans le compagnon.
3. Renseignez les poids actuels des bobines A1 à A4.
4. Dans la carte **Passerelle Bambu Studio**, vérifiez la correspondance de
   secours entre les filaments du projet et A1–A4.
5. Tranchez puis lancez normalement l’impression depuis Bambu Studio officiel.
6. Vérifiez que Companion affiche **Travail armé automatiquement**.

Bambu Studio crée lui-même un `.gcode.3mf` temporaire lors de l’envoi : la
passerelle le récupère sans export manuel et utilise la correspondance A1–A4
enregistrée. Le canal des commandes MQTT n’est pas surveillé, car certains
firmwares A1 mini ferment alors la connexion des clients tiers. L’ancien import
manuel reste disponible dans le tableau de bord.

La déduction est effectuée une fois seulement à la réception de `FINISH`.
Une impression annulée ou échouée n’est pas débitée.

## Données et dépannage

Les données sont enregistrées avec des droits privés dans :

```text
~/Library/Application Support/AMS Lite Companion/state.json
```

Journal lisible :

```text
~/Library/Application Support/AMS Lite Companion/companion.log
```

Pour vérifier un fichier sans lancer l’interface :

```bash
python3 ams_companion.py --parse /chemin/vers/plateau.gcode.3mf
```

La mesure est une estimation du trancheur, car l’AMS Lite ne publie pas de
poids réel. Le poids peut être corrigé manuellement à tout moment.
