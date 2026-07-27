# Faisabilité du gardien de plateau

## Décision actuelle

V2 sépare volontairement deux capacités :

1. détecter, conserver les preuves et alerter ;
2. annuler un objet précis.

La première est développable localement. V3 conserve une option explicitement
manuelle qui prépare et journalise une instruction unitaire `skip_objects`
avec l’identifiant canonique du fichier `slice_info.config`, puis la publie
uniquement après confirmation explicite pendant une impression MQTT connectée.
Elle emploie le protocole LAN observé dans Bambu Studio ; ce mécanisme reste à
valider sur le modèle et le firmware réellement utilisés avant d’être considéré
comme confirmé par l’imprimante.

Le dépôt officiel de Bambu Studio référence encore une demande ouverte pour
« Skip Object » sur les impressions SD/LAN :
<https://github.com/bambulab/BambuStudio/issues/3098>. Le dépôt précise aussi
que sa couche réseau Bambu s'appuie sur des bibliothèques non libres ; V2 ne
copiera ni ne simulera donc une commande de contrôle non documentée :
<https://github.com/bambulab/BambuStudio>.

## Conséquence produit

Le noyau `plate_guardian.py` ne peut que créer une proposition avec preuves.
`autopilot.py` produit d’abord une alerte ; une instruction locale immuable et
idempotente n’est créée qu’après un clic explicite de l’utilisateur. Le Core
la publie une seule fois sur la session MQTT déjà connectée, sans persistance
de file : une coupure annule la demande au lieu de la rejouer. Les popups V3
servent uniquement à attirer l’attention et ne proposent aucune exécution.

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
