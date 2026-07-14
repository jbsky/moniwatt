#!/usr/bin/env python3

import json
import time
import os
import signal
import logging
import queue
import threading
import serial
import serial.tools.list_ports

import paho.mqtt.client as mqtt
import pickle

import json

with open('/etc/moniwatt/sendPower.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

# Accès aux valeurs
BROKER = config['mqtt']['broker']
PORT = config['mqtt']['port']
USERNAME = config['mqtt']['username']
PASSWORD = config['mqtt']['password']
BASE_TOPIC = config['mqtt']['base_topic']

MODEL = config['device']['model']
MANUFACTURER = config['device']['manufacturer']
DEVICE_NAME = f"{MODEL}_electricity"

DEBUG = config['system']['debug']
LOG_FILE = config['system']['log_file']
ENERGY_SAVE_FILE = config['system']['energy_save_file']
BAUDRATE = config['system']['baudrate']

VOLTAGE_RMS = config['electrical']['voltage_rms']
SEND_INTERVAL = config['electrical']['send_interval']

sct = config['sensors']

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

def find_serial_port():
    """Trouve automatiquement le port série disponible"""
    
    # Lister tous les ports série disponibles
    for port_info in serial.tools.list_ports.comports():
        port = port_info.device
        if port.startswith('/dev/ttyACM'):
            logger.info(f"Port ttyACM trouvé: {port}")
            return port
    
    raise Exception("Aucun port ttyACM trouvé")

class SensorMonitor:
    def __init__(self):
        self.mqtt_queue = queue.Queue()
        self.running = True
        self.total_processed = 0
        self.run_time = time.time()
        self.config_timestamps = {}
        self.last_sample_time = {}
        self.show_sensor_stats = {}
        for sensor_name in sct:
            self.last_sample_time[sensor_name] = self.run_time
            self.show_sensor_stats[sensor_name] = False

        # Stockage temporaire des données pour agrégation
        self.current_readings = {}
        self.reading_count = {}
        
        # Energy accumulation tracking dictionary
        self.energy_totals = {}

        # Live sequence/diagnostic state (reset every process start; not itself loaded from pickle)
        self.last_arduino_idx = None
        self.last_seen_ts = None
        self.last_power = {}
        self.last_cum = {}

        # Cumulative diagnostic counters — default 0 here, load_energy_totals() overwrites
        # them if a new-format pickle is found, so they persist/grow across restarts.
        self.missed_windows_total = 0
        self.arduino_resets = 0

        # Snapshot of what the previous run last saw, used once at startup for resync/backfill.
        self.persisted_arduino_idx = None
        self.persisted_last_seen_ts = None
        self.persisted_last_power = {}
        self.persisted_last_cum = {}

        self.load_energy_totals()
        
        # Connect to MQTT
        self.mqtt_client = mqtt.Client(client_id=f"sensor_monitor_{os.getpid()}", clean_session=False)
        self.mqtt_client.username_pw_set(USERNAME, PASSWORD)
        
        # Configure MQTT QoS and retention
        self.mqtt_client.message_retry_set(5)  # Tentatives de renvoi
        
        # Start MQTT publishing thread
        self.mqtt_thread = threading.Thread(target=self.mqtt_worker)
        self.mqtt_thread.daemon = True
        self.mqtt_thread.start()
        
        # Register signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        # Start periodic save thread
        self.save_thread = threading.Thread(target=self.periodic_save)
        self.save_thread.daemon = True
        self.save_thread.start()
        
        # Démarrer thread d'agrégation et d'envoi périodique
        self.aggregation_thread = threading.Thread(target=self.periodic_publish)
        self.aggregation_thread.daemon = True
        self.aggregation_thread.start()

    def load_energy_totals(self):
        """Load accumulated energy values from file if available"""
        try:
            if os.path.exists(ENERGY_SAVE_FILE):
                with open(ENERGY_SAVE_FILE, 'rb') as f:
                    loaded = pickle.load(f)
                    if isinstance(loaded, dict) and "energy_totals" in loaded:
                        self.energy_totals = loaded.get("energy_totals", {})
                        self.persisted_arduino_idx = loaded.get("arduino_idx")
                        self.persisted_last_seen_ts = loaded.get("last_seen_ts")
                        self.persisted_last_power = loaded.get("last_power", {})
                        self.persisted_last_cum = loaded.get("last_cum", {})
                        self.missed_windows_total = loaded.get("missed_windows_total", 0)
                        self.arduino_resets = loaded.get("arduino_resets", 0)
                    else:
                        # Legacy flat format: {sensor_name: float}
                        self.energy_totals = loaded
                    logger.info(f"Loaded energy totals from {ENERGY_SAVE_FILE}: {self.energy_totals}")

                                # Initialize sensor in energy_totals if not exists
                    for sensor_name in sct:
                        if sensor_name not in self.energy_totals:
                            self.energy_totals[sensor_name] = 0.0
                            logger.info(f"{sensor_name} starting with zero")
                        else:
                            # if todo self.energy_totals[sensor_name] = 0.0
                            logger.info(f"{sensor_name} starting with {self.energy_totals[sensor_name]}")


            else:
                logger.warning("No saved energy totals found, starting with zero")
                self.energy_totals = {}
                
                # Initialize with zero for all sensors
                for sensor_name in sct:
                    self.energy_totals[sensor_name] = 0.0
        except Exception as e:
            logger.error(f"Error loading energy totals: {e}")
            self.energy_totals = {sensor_name: 0.0 for sensor_name in sct}

    def save_energy_totals(self):
        """Save accumulated energy values to file"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(ENERGY_SAVE_FILE), exist_ok=True)
            
            save_data = {
                "energy_totals": self.energy_totals,
                "arduino_idx": self.last_arduino_idx,
                "last_seen_ts": self.last_seen_ts,
                "last_power": self.last_power,
                "last_cum": self.last_cum,
                "missed_windows_total": self.missed_windows_total,
                "arduino_resets": self.arduino_resets,
            }
            with open(ENERGY_SAVE_FILE, 'wb') as f:
                pickle.dump(save_data, f)
            if DEBUG:
                logger.debug(f"Saved energy totals to {ENERGY_SAVE_FILE}: {self.energy_totals}")
        except Exception as e:
            logger.error(f"Error saving energy totals: {e}")
            
    def periodic_save(self):
        """Periodically save energy totals"""
        while self.running:
            time.sleep(300)  # Save every 5 minutes instead of every minute
            self.save_energy_totals()

    def connect_mqtt(self):
        try:
            self.mqtt_client.connect(BROKER, PORT)
            self.mqtt_client.loop_start()
            logger.info(f"Connected to MQTT broker at {BROKER}:{PORT}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            return False

    def mqtt_worker(self):
        """Worker thread for MQTT publishing"""
        # Tentative de connexion initiale, mais on continue même si ça échoue
        connected = self.connect_mqtt()
        if not connected:
            logger.warning("Initial MQTT connection failed, will retry later")
        
        while self.running:
            try:
                # Si pas connecté, on tente de se reconnecter
                if not connected or not self.mqtt_client.is_connected():
                    logger.info("Attempting to reconnect to MQTT broker...")
                    connected = self.connect_mqtt()
                    if not connected:
                        time.sleep(5)  # Attendre avant de réessayer
                        continue
                
                # Get message from queue with timeout
                try:
                    topic, payload, qos = self.mqtt_queue.get(timeout=1.0)
                    self.mqtt_client.publish(topic, payload, qos=qos, retain=(qos > 0))
                    self.mqtt_queue.task_done()
                except queue.Empty:
                    continue
                    
            except Exception as e:
                logger.error(f"Error in MQTT worker: {e}")
                connected = False  # Marquer comme déconnecté pour forcer une reconnexion
                time.sleep(1)

    def periodic_publish(self):
        """Agrégation des données et publication périodique"""
        while self.running:
            # Attendre l'intervalle d'envoi
            time.sleep(SEND_INTERVAL)
            
            # Publier les données agrégées
            self.publish_aggregated_data()
            self.publish_diagnostics_data()

    def publish_aggregated_data(self):
        """Publier les données agrégées pour tous les capteurs"""
        try:
            current_time = time.time()
            for sensor_name in self.current_readings:
                if sensor_name not in self.reading_count or self.reading_count[sensor_name] == 0:
                    continue
                
                # Calculer les moyennes
                avg_readings = {}
                for key, value in self.current_readings[sensor_name].items():
                    if key == "energy":
                        avg_readings[key] = value
                    else:
                        avg_readings[key] = value / self.reading_count[sensor_name]

                power = round(avg_readings.get("power", 0), 1)
                current = round(avg_readings.get("current", 0), 3)
                
                # Publish state as JSON on old master topic format
                # This keeps entity_id = sensor.pi0_electricity_<desc_sanitized>
                state_topic = f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_{sensor_name}/state"
                payload = json.dumps({"power": power, "current": current})
                self.mqtt_queue.put((state_topic, payload, 1))
                
                if DEBUG:
                    if self.show_sensor_stats[sensor_name]:
                        logger.debug(f"Published {sensor_name}: P={power}W I={current}A")
                        self.show_sensor_stats[sensor_name] = False
                    
                # Vérifier si nous devons publier la configuration (toutes les 6 heures)
                last_config_time = self.config_timestamps.get(sensor_name, 0)
                
                if current_time - last_config_time > 21600:
                    self.publish_device_config(DEVICE_NAME, sensor_name)
                    self.config_timestamps[sensor_name] = current_time
            
                # Réinitialiser les compteurs
                self.reading_count[sensor_name] = 0
            
        except Exception as e:
            logger.error(f"Error publishing aggregated data: {e}")

    def publish_diagnostics_data(self):
        """Publier les compteurs de diagnostic (fenêtres manquées, resets Arduino)"""
        try:
            current_time = time.time()
            state_topic = f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_diagnostics/state"
            payload = json.dumps({
                "missed_windows": self.missed_windows_total,
                "arduino_resets": self.arduino_resets,
            })
            self.mqtt_queue.put((state_topic, payload, 1))

            last_config_time = self.config_timestamps.get("_diagnostics", 0)
            if current_time - last_config_time > 21600:
                self.publish_diagnostics_config(DEVICE_NAME)
                self.config_timestamps["_diagnostics"] = current_time
        except Exception as e:
            logger.error(f"Error publishing diagnostics data: {e}")

    def check_arduino_sequence(self, json_data, current_time):
        """Detect gaps (dropped serial lines) and resets (Arduino reboot) via the
        incrementing 'idx' field. Also performs a one-time startup resync against
        state persisted from the previous run: if the Arduino kept running (the
        common case, since DTR/RTS are disabled), backfill EXACT energy from the
        delta of its cumulative counters; only if the Arduino itself rebooted (no
        real measurement exists for that gap) fall back to an explicitly-labeled
        estimate based on the last known power held constant."""
        try:
            idx = json_data.get("idx")
            if idx is None:
                return  # older firmware or {"event": ...} lines

            # Track the freshest raw cumulative counters regardless of idx continuity,
            # so the next resync (if any) has the best possible baseline.
            for sensor_name in sct:
                cum_value = json_data.get(sensor_name.replace("adc", "cum"))
                if cum_value is not None:
                    self.last_cum[sensor_name] = cum_value

            if self.last_arduino_idx is None:
                if self.persisted_arduino_idx is not None:
                    if idx < self.persisted_arduino_idx:
                        logger.warning(f"Arduino reset detected during service downtime "
                                        f"(idx {idx} < persisted {self.persisted_arduino_idx}) — "
                                        f"downtime energy will be ESTIMATED, not measured")
                        self.arduino_resets += 1
                        self._backfill_estimated_energy(current_time)
                    else:
                        missed = idx - self.persisted_arduino_idx - 1
                        if missed > 0:
                            logger.warning(f"{missed} window(s) not received while service was down "
                                            f"(persisted idx {self.persisted_arduino_idx} -> {idx})")
                            self.missed_windows_total += missed
                        self._backfill_exact_energy(json_data)

                    # Avoid double-counting the reconnect sliver in process_sensor_data's
                    # own time_diff accumulation (last_sample_time still holds process-start).
                    for sensor_name in sct:
                        self.last_sample_time[sensor_name] = current_time
            else:
                if idx < self.last_arduino_idx:
                    logger.warning(f"Arduino reset detected (idx {idx} < last {self.last_arduino_idx})")
                    self.arduino_resets += 1
                else:
                    missed = idx - self.last_arduino_idx - 1
                    if missed > 0:
                        logger.warning(f"{missed} window(s) missed (idx {self.last_arduino_idx} -> {idx})")
                        self.missed_windows_total += missed

            self.last_arduino_idx = idx
            self.last_seen_ts = current_time
        except Exception as e:
            logger.error(f"Error checking arduino sequence: {e}")

    def _backfill_exact_energy(self, json_data):
        """Arduino stayed alive across the gap: compute EXACT energy from the delta
        of its raw cumulative counters (centivolt-seconds, integrated on-device against
        real micros() elapsed time — no guessing involved, and no assumed sample rate)."""
        for sensor_name in sct:
            old_cum = self.persisted_last_cum.get(sensor_name)
            new_cum = json_data.get(sensor_name.replace("adc", "cum"))
            if old_cum is None or new_cum is None:
                continue
            delta_cum = (new_cum - old_cum) & 0xFFFFFFFF  # wraparound-safe (uint32 on Arduino)
            amp_ratio = sct.get(sensor_name, {}).get("amp")
            if not amp_ratio:
                continue
            volt_seconds = delta_cum / 100.0
            energy_wh = VOLTAGE_RMS * amp_ratio * volt_seconds / 3600.0
            self.energy_totals[sensor_name] = self.energy_totals.get(sensor_name, 0.0) + energy_wh
            logger.info(f"Rectificatif exact {sensor_name}: +{energy_wh:.3f} Wh "
                        f"(delta cumulatif Arduino, pas une estimation)")

    def _backfill_estimated_energy(self, current_time):
        """Arduino itself rebooted: no real measurement exists for the gap, fall back
        to holding the last known power constant — clearly an estimate, not a measurement."""
        if self.persisted_last_seen_ts is None:
            return
        downtime_s = current_time - self.persisted_last_seen_ts
        if downtime_s <= 0:
            return
        for sensor_name, last_power in self.persisted_last_power.items():
            estimated_wh = last_power * (downtime_s / 3600.0)
            self.energy_totals[sensor_name] = self.energy_totals.get(sensor_name, 0.0) + estimated_wh
            logger.info(f"ESTIMATION {sensor_name}: +{estimated_wh:.3f} Wh (~{downtime_s:.0f}s à "
                        f"P={last_power}W — Arduino aussi redémarré, aucune mesure réelle disponible)")

    def process_sensor_data(self, json_data, sensor_name, current_time):
        """Process the sensor data and aggregate"""
        try:

            # Get sensor value
            rms = float(json_data.get(sensor_name, 0))
            if not rms:
                return
            
            # Get sensor ratio
            sct_ratio = sct.get(sensor_name, {}).get("amp")

            # Calculations
            current = abs(rms * sct_ratio)
            abs_power = abs(VOLTAGE_RMS * current)
            
            time_diff = current_time - self.last_sample_time[sensor_name]
            self.last_sample_time[sensor_name] = current_time
            
            # Calculate energy for this time period (Wh) and Accumulate energy
            self.energy_totals[sensor_name] += abs_power * (time_diff / 3600.0)  # Convert seconds to hours
            total_energy = self.energy_totals[sensor_name]

            # Format for precision
            voltage = round(rms, 6)
            current = round(current, 6)
            abs_power = round(abs_power, 6)
            total_energy = round(total_energy, 6)

            self.last_power[sensor_name] = abs_power
            
            # Stocker pour agrégation au lieu de publier immédiatement
            if sensor_name not in self.current_readings or self.reading_count[sensor_name] == 0:
                self.current_readings[sensor_name] = {
                    "voltage": voltage,
                    "current": current,
                    "power": abs_power,
                    "energy": total_energy
                }
                self.reading_count[sensor_name] = 1
            else:
                for key, value in [("voltage", voltage), ("current", current), 
                                  ("power", abs_power), ("energy", total_energy)]:
                    if key == "energy":
                        self.current_readings[sensor_name][key] = value
                    else:
                        self.current_readings[sensor_name][key] += value
                self.reading_count[sensor_name] += 1
            
            # Mise à jour des statistiques de traitement
            if DEBUG:
                self.total_processed += 1
                if self.total_processed % 100 == 0:  # Réduire la fréquence des logs
                    new_time = time.time()
                    elapsed = new_time - self.run_time
                    self.run_time = new_time
                    rate = self.total_processed / elapsed
                    logger.debug(f"Processing rate: {rate:.2f} messages/second (total: {self.total_processed})")
                    self.total_processed = 0
                    for list_name in sct:
                        self.show_sensor_stats[list_name] = True
            
        except Exception as e:
            logger.error(f"Error processing sensor data: {e}")

    def publish_device_config(self, device_name, sensor_name):
        """Publish HA MQTT discovery matching old entity_id format"""
        try:
            desc = sct.get(sensor_name, {}).get("desc", sensor_name)
            state_topic = f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_{sensor_name}/state"

            device_config = {
                "identifiers": [f"{MANUFACTURER}_{MODEL}_electricity"],
                "model": MODEL,
                "manufacturer": MANUFACTURER,
                "name": DEVICE_NAME
            }

            configs = [
                {
                    "topic": f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_{sensor_name}_power/config",
                    "payload": {
                        "device_class": "power",
                        "name": desc,
                        "object_id": f"{DEVICE_NAME}_{sensor_name}_power",
                        "unique_id": f"{DEVICE_NAME}_{sensor_name}_power",
                        "unit_of_measurement": "W",
                        "state_class": "measurement",
                        "state_topic": state_topic,
                        "value_template": "{{ value_json.power }}",
                        "device": device_config,
                        "expire_after": SEND_INTERVAL * 5
                    }
                },
                {
                    "topic": f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_{sensor_name}_current/config",
                    "payload": {
                        "device_class": "current",
                        "name": f"{desc} Current",
                        "object_id": f"{DEVICE_NAME}_{sensor_name}_current",
                        "unique_id": f"{DEVICE_NAME}_{sensor_name}_current",
                        "unit_of_measurement": "A",
                        "state_class": "measurement",
                        "state_topic": state_topic,
                        "value_template": "{{ value_json.current }}",
                        "device": device_config,
                        "expire_after": SEND_INTERVAL * 5
                    }
                },
            ]

            for cfg in configs:
                self.mqtt_queue.put((cfg["topic"], json.dumps(cfg["payload"]), 0))

            if DEBUG:
                logger.debug(f"Published discovery for {desc}")

        except Exception as e:
            logger.error(f"Error publishing config for {sensor_name}: {e}")

    def publish_diagnostics_config(self, device_name):
        """Publish HA MQTT discovery for Arduino link diagnostic sensors"""
        try:
            state_topic = f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_diagnostics/state"
            device_config = {
                "identifiers": [f"{MANUFACTURER}_{MODEL}_electricity"],
                "model": MODEL,
                "manufacturer": MANUFACTURER,
                "name": DEVICE_NAME
            }
            configs = [
                {
                    "topic": f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_diagnostics_missed_windows/config",
                    "payload": {
                        "name": "Arduino fenêtres perdues",
                        "object_id": f"{DEVICE_NAME}_diagnostics_missed_windows",
                        "unique_id": f"{DEVICE_NAME}_diagnostics_missed_windows",
                        "state_class": "total_increasing",
                        "entity_category": "diagnostic",
                        "state_topic": state_topic,
                        "value_template": "{{ value_json.missed_windows }}",
                        "device": device_config,
                        "expire_after": SEND_INTERVAL * 5
                    }
                },
                {
                    "topic": f"{BASE_TOPIC}/sensor/{DEVICE_NAME}_diagnostics_arduino_resets/config",
                    "payload": {
                        "name": "Arduino resets",
                        "object_id": f"{DEVICE_NAME}_diagnostics_arduino_resets",
                        "unique_id": f"{DEVICE_NAME}_diagnostics_arduino_resets",
                        "state_class": "total_increasing",
                        "entity_category": "diagnostic",
                        "state_topic": state_topic,
                        "value_template": "{{ value_json.arduino_resets }}",
                        "device": device_config,
                        "expire_after": SEND_INTERVAL * 5
                    }
                },
            ]
            for cfg in configs:
                self.mqtt_queue.put((cfg["topic"], json.dumps(cfg["payload"]), 0))
            if DEBUG:
                logger.debug("Published discovery for diagnostics sensors")
        except Exception as e:
            logger.error(f"Error publishing diagnostics config: {e}")

    def run(self):
        """Main loop to read and process data"""
        if DEBUG:
            logger.debug("Current sensor monitoring started")
        
        try:
            port = find_serial_port()
            logger.info(f"Utilisation du port série: {port}")
            # Open serial connection
            with serial.Serial(
                port=port, 
                baudrate=BAUDRATE, 
                timeout=0.6,  # Slightly more than Arduino emit interval (465ms)
                # CRITICAL: Don't reset the device on connection
                dsrdtr=False,  # Disable DSR/DTR
                rtscts=False   # Disable RTS/CTS
            ) as ser:
                # Don't set DTR/RTS which would reset Arduino
                ser.dtr = False
                ser.rts = False
                ser.reset_input_buffer()  # Final flush
                
                while self.running:
                    try:
                        # readline() blocks until data arrives or timeout (1s)
                        # No sleep needed — blocking I/O = 0% CPU while waiting
                        data = ser.readline()
                        if DEBUG:
                            logger.debug(f"Read {data} from serial")
                        
                        if not data:
                            # Serial timeout, no data — loop back (no sleep needed)
                            continue
                        
                            
                        # Decode et parse directement
                        try:
                            decoded_str = data.decode('utf-8', errors='replace').strip()
                            
                            # Ignore les lignes vides
                            if not decoded_str:
                                continue
                            # Parse le JSON
                            json_data = json.loads(decoded_str)
                            # Calculate time since last sample for energy accumulation
                            current_time = time.time()
                            self.check_arduino_sequence(json_data, current_time)
                            # Process each configured sensor
                            for sensor_name in sct:
                                self.process_sensor_data(json_data, sensor_name, current_time)
                                
                        except json.JSONDecodeError:
                            logger.error(f"Invalid JSON line ignored: {decoded_str[:100]}")
                        except UnicodeDecodeError as ue:
                            logger.error(f"Unicode decode error: {ue}")
                        
                    except serial.SerialException as se:
                        logger.error(f"Serial exception: {se}")
                        self.save_energy_totals()
                        break
                    except Exception as e:
                        logger.error(f"Error in main loop: {e}")
                        time.sleep(0.1)
                        
        except serial.SerialException as e:
            logger.error(f"Cannot open serial port: {e}")
        except Exception as e:
            logger.error(f"Serial port error: {e}")
            
    def signal_handler(self, sig, frame):
        """Handle termination signals"""
        logger.info("Shutting down...")
        self.running = False
        
        # Save energy totals before exit
        self.save_energy_totals()
        
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()
        time.sleep(1)  # Give time for threads to finish
        
if __name__ == "__main__":
    monitor = SensorMonitor()
    # Démarrer thread d'agrégation et d'envoi périodique
    monitor.run()
