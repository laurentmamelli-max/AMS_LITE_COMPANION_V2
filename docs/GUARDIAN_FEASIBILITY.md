# Faisabilité du gardien de plateau

## Décision actuelle

V2 sépare volontairement deux capacités :

1. détecter, conserver les preuves et alerter ;
2. annuler un objet précis.

La première est développable localement. La seconde n'est pas activée : le
projet ne dispose pas d'une commande Bambu LAN/SD officielle et vérifiée pour
ignorer un objet d'une impression en cours.

Le dépôt officiel de Bambu Studio référence encore une demande ouverte pour
« Skip Object » sur les impressions SD/LAN :
<https://github.com/bambulab/BambuStudio/issues/3098>. Le dépôt précise aussi
que sa couche réseau Bambu s'appuie sur des bibliothèques non libres ; V2 ne
copiera ni ne simulera donc une commande de contrôle non documentée :
<https://github.com/bambulab/BambuStudio>.

## Conséquence produit

Le noyau `plate_guardian.py` ne peut que créer une proposition avec preuves.
Il n'expose volontairement aucune méthode d'annulation. Une future commande ne
pourra être ajoutée qu'après :

1. validation du protocole par Bambu pour le modèle et le firmware ciblés ;
2. test sur un plateau de démonstration ;
3. confirmation explicite dans l'interface ;
4. journal idempotent de la commande et de son résultat.

## Détection

La caméra locale est un prérequis distinct. Bambu Studio offre du contrôle et
de la surveillance, mais son flux caméra local n'est pas présenté comme une API
publique stable. Le gardien accepte donc des observations normalisées venant
d'un futur adaptateur caméra ou d'une caméra externe, sans lier la logique de
sécurité à un protocole vidéo non validé.

Le module `bambu_camera.py` isole le protocole communautaire A1/P1 sur le port
local 6000. Il est strictement en lecture seule et refuse toute caméra dont le
certificat TLS n'a pas été préalablement épinglé. Cette couche ne sera activée
sur une imprimante réelle qu'après un test de connexion explicite.
