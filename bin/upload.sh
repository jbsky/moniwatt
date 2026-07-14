#!/bin/bash
#
# Usage: upload.sh [port]
# Port par défaut : /dev/ttyACM0 (celui qu'on cible AVANT le reset — le device
# peut ré-énumérer sous un autre nom après, cf. détection ci-dessous).

PORT="${1:-/dev/ttyACM0}"
SKETCH="/root/ads1115"

echo "=== Libération du port ($PORT) ==="
fuser -k $PORT 2>/dev/null
sleep 1

echo "=== Reset du Mega ==="
# Méthode 1: stty (touch 1200 bauds — nécessaire pour ce Mega depuis ce Pi,
# le toggle DTR standard d'avrdude seul ne suffit pas à activer le bootloader)
stty -F $PORT 1200 2>/dev/null
sleep 0.5

# Méthode 2: Python si stty échoue
python3 -c "
import serial
import time
try:
    s = serial.Serial('$PORT', 1200)
    s.setDTR(False)
    time.sleep(0.5)
    s.close()
    time.sleep(0.5)
except:
    pass
" 2>/dev/null

echo "=== Ré-énumération USB (le port peut changer après reset) ==="
sleep 2
echo "Ports ttyACM présents: $(ls /dev/ttyACM* 2>/dev/null)"
DETECTED_PORT=$(ls /dev/ttyACM* 2>/dev/null | head -1)
if [ -z "$DETECTED_PORT" ]; then
    echo "Aucun port ttyACM détecté après reset, abandon."
    exit 1
fi
if [ "$DETECTED_PORT" != "$PORT" ]; then
    echo "Port changé après reset: $PORT -> $DETECTED_PORT"
fi
PORT="$DETECTED_PORT"

echo "=== Compilation + Upload (port: $PORT) ==="
# Compile et upload en une seule commande arduino-cli : un compile puis un
# upload séparés (--input-dir) ne retrouvent pas l'artefact de build (chemin
# de build temporaire différent d'un appel à l'autre).
~/bin/arduino-cli compile --fqbn arduino:avr:mega --port $PORT --upload $SKETCH --verbose
UPLOAD_RC=$?

echo "=== Terminé ==="
exit $UPLOAD_RC
