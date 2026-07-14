# Moniwatt — compteur électrique 4 canaux (Pi Zero + Arduino Mega + ADS1115)

Mesure la consommation électrique de 4 circuits via pinces ampèremétriques (SCT), publie vers
Home Assistant en MQTT (discovery auto), et intègre l'énergie via un helper Riemann Sum côté HA.

Fait suite au tutoriel [passionelectronique.fr — Tutorial ADS1115](https://passionelectronique.fr/tutorial-ads1115/).

## Hardware

- Raspberry Pi Zero W (`pi0compteur.home.arpa`)
- Arduino Mega 2560, connecté en USB (`/dev/ttyACM0`, 115200 baud)
- 4x ADS1115 (ADC 16 bits) aux adresses I2C 0x48, 0x49, 0x4A, 0x4B
- 4x pinces SCT (sortie tension, burden interne, 1V à courant nominal), actuellement câblées

## Structure du repo

- `ads1115/ads1115.ino` — sketch Arduino : lecture différentielle des 4 ADS1115 (mode continu),
  calcul RMS par fenêtre glissante, compteur de séquence (`idx`) et compteur cumulatif d'énergie
  brute (`cum1`..`cum4`) par canal, émis en JSON sur le port série.
- `bin/sendPower.py` — service Python : lit le flux JSON série, calcule puissance/courant par
  canal, publie en MQTT avec Home Assistant discovery, persiste l'état (énergie cumulée,
  diagnostics) en pickle, et gère la resynchronisation au démarrage (voir plus bas).
- `bin/sendPower.json.example` — template de config (MQTT, capteurs, calibration). À copier vers
  `/etc/moniwatt/sendPower.json` sur le Pi avec les vraies valeurs (jamais committé).
- `bin/upload.sh` — flash l'Arduino depuis le Pi (reset bootloader "touch 1200 bauds" +
  compile/upload `arduino-cli` en une seule commande).
- `bin/restart` — redémarre le service `compteur-mqtt` (systemd user, utilisateur `compteur`).
- `bin/arduino-cli` — binaire ARM, pour exécution directe sur le Pi Zero.
- `playbook.yaml` — déploiement Ansible complet (service systemd, `sendPower.py`, sketch Arduino,
  flash automatique si le sketch a changé). Générique : cible le groupe `moniwatt`, aucune valeur
  (user, nom de service, chemins, port série) n'est en dur — voir Déploiement.
- `inventory.ini` / `host_vars/*.yml` — inventaire du groupe `moniwatt` et overrides par instance.

## Déploiement

```bash
ansible-playbook playbook.yaml
```

`ansible.cfg` pointe vers `inventory.ini` (groupe `moniwatt`), donc la commande ci-dessus suffit —
pas besoin de `-i`. Idempotent : ne redéploie/reflashe que ce qui a changé (pattern
`notify`/handlers). Le flash Arduino stoppe proprement le service Python avant (conflit sur le
port série), reflashe, puis le redémarre.

Le playbook lit ses variables via `pre_tasks` (`set_fact` + `default(...)`, jamais un `vars:` de
play — sinon les valeurs de host_vars seraient ignorées) : `moniwatt_user`, `moniwatt_uid`,
`moniwatt_service_name`, `moniwatt_service_description`, `arduino_serial_port`,
`moniwatt_bin_dest`, `moniwatt_arduino_dest`, `moniwatt_upload_script_dest`. Toutes ont un défaut
sensé ; seul `moniwatt_service_description` est surchargé aujourd'hui, dans
`host_vars/pi0compteur.home.arpa.yml`.

### Ajouter une nouvelle instance (ex. "Compteur 3")

1. Ajouter le hostname dans `inventory.ini`, sous `[moniwatt]`.
2. Créer `host_vars/<hostname>.yml` avec au minimum `moniwatt_service_description: "Compteur 3 MQTT"`
   (et `arduino_serial_port`/`moniwatt_uid` si ce Pi diffère des défauts).
3. `ansible-playbook playbook.yaml --limit <hostname>`.

## Branchement SCT

Différentiel direct par ADS1115 : chaque pince SCT sur AIN2/AIN3 (1 SCT par chip). Torsader les
fils, câbles courts. Montage détaillé (alimentation ADS1115, câblage SCT) :
[passionelectronique.fr — Tutorial ADS1115](https://passionelectronique.fr/tutorial-ads1115/).

## Scalabilité (jusqu'à 8 canaux)

Chaque ADS1115 a 2 paires différentielles (AIN0-AIN1 et AIN2-AIN3) ; seule AIN2-AIN3 est câblée
et lue par chip aujourd'hui (`ads1115.ino`, mode continu verrouillé sur ce seul mux). Passer à
8 canaux demande donc deux choses, pas juste du câblage :
- câbler aussi AIN0-AIN1 sur les 4 chips,
- adapter le sketch pour servir les deux paires par chip (alternance de mux ou second passage en
  mode continu), ce qui a un impact sur le débit d'échantillonnage actuel (~379 sps mesurés, voir
  historique des commits) — pas encore implémenté.

Côté Python, `sendPower.py` est déjà entièrement piloté par la config (`for sensor_name in sct`
partout, aucun `adc1`..`adc4` en dur) : ajouter des canaux ne demande aucun changement de code,
juste étendre `sensors` dans `sendPower.json` :

```json
"sensors": {
  "adc1": {"amp": 5,  "desc": "Circuit 1"},
  "adc2": {"amp": 10, "desc": "Circuit 2"},
  "adc3": {"amp": 20, "desc": "Circuit 3"},
  "adc4": {"amp": 5,  "desc": "Circuit 4"},
  "adc5": {"amp": 5,  "desc": "Circuit 5"},
  "adc6": {"amp": 10, "desc": "Circuit 6"},
  "adc7": {"amp": 20, "desc": "Circuit 7"},
  "adc8": {"amp": 5,  "desc": "Circuit 8"}
}
```

## Resynchronisation au démarrage (idx / cum)

Le service Python désactive volontairement DTR/RTS pour que l'Arduino ne redémarre jamais quand
Python se reconnecte au port série — il continue de tourner indépendamment de l'état du service
(`Restart=always` en systemd rend les redémarrages fréquents, pas juste des pannes rares).

- L'Arduino incrémente un compteur de séquence `idx` par fenêtre RMS émise, et un compteur
  cumulatif `cum1`..`cum4` (intégrale réelle du signal RMS contre `micros()`, indépendant de la
  calibration côté Python).
- Au (re)démarrage, `sendPower.py` compare l'`idx` reçu à l'état persisté :
  - Si l'Arduino n'a pas rebooté entre-temps (cas courant, USB jamais coupé) : l'énergie manquée
    est reconstituée **exactement** via le delta du compteur `cum`.
  - Si l'Arduino a aussi rebooté (`idx` reparti à 0) : aucune mesure réelle n'existe pour ce trou,
    fallback sur une estimation (dernière puissance connue × durée), explicitement loguée comme
    telle.
- Compteurs cumulatifs `missed_windows_total` / `arduino_resets` persistés et exposés en
  diagnostic MQTT (entités Home Assistant dédiées, `entity_category: diagnostic`).
