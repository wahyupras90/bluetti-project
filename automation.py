#!/usr/bin/env python3
"""
automation.py — Bluetti Elite 200 V2 Automation
7 rule deterministik, tanpa Home Assistant

Jalankan: python3 automation.py
Service : automation.service (systemd)

Keputusan desain:
  A2 menang di boundary 40% (proteksi baterai > backup)
  A5 dua guard: malam + PLN ada (tidak melumpuhkan A6 saat outage)
  A6 SOC > 41 (gap dengan A2, tidak overlap)
  Interval polling 60s produksi, 30s debug
"""

import os
import time
import logging
from datetime import datetime
import paho.mqtt.client as mqtt

# ================================================================
# KONFIGURASI — sesuaikan sebelum deploy
# ================================================================
MQTT_BROKER  = "127.0.0.1"
MQTT_PORT    = 1883
DEVICE_NAME  = "PR200V2-2551110318791"  # confirmed dari mosquitto_sub Pi

TOPIC_SOC    = f"bluetti/state/{DEVICE_NAME}/total_battery_percent"
TOPIC_PV     = f"bluetti/state/{DEVICE_NAME}/dc_input_power"
TOPIC_AC_OUT = f"bluetti/state/{DEVICE_NAME}/ac_output_power"
TOPIC_GRID_V = f"bluetti/state/{DEVICE_NAME}/ac_input_voltage"
TOPIC_AC_ON  = f"bluetti/state/{DEVICE_NAME}/ac_output_on"
TOPIC_CMD    = f"bluetti/command/{DEVICE_NAME}/ac_output_on"

LOG_FILE       = os.path.expanduser("~/bluetti_log.txt")
LAST_RULE_FILE = os.path.expanduser("~/bluetti_last_rule.txt")
PAUSE_FLAG     = "/tmp/automation_paused"

DEBOUNCE_SEC   = 120   # jeda minimum antar trigger rule yang sama

# ================================================================
# LOGGING KE STDOUT (ditangkap systemd journalctl)
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ================================================================
# STATE
# ================================================================
state = {
    "soc":     None,   # float %
    "pv":      None,   # float W
    "ac_out":  None,   # float W
    "grid_v":  None,   # float V
    "ac_on":   None,   # "ON" / "OFF"
}

# Timer A4 (solar lemah 5 menit)
_a4_timer_start = None
_a4_fired       = False

# Debounce: waktu terakhir tiap rule trigger
_last_trigger = {f"A{i}": 0.0 for i in range(1, 8)}

# ================================================================
# HELPER
# ================================================================
def now_sec():
    return time.time()

def hms():
    return datetime.now().strftime("%H:%M:%S")

def hour():
    return datetime.now().hour

def is_daytime():
    """Siang: 09:00 – 16:00"""
    h = hour()
    return 9 <= h < 16

def is_nighttime():
    """Malam: 16:00 – 09:00"""
    h = hour()
    return h >= 16 or h < 9

def ac_is_on():
    return state["ac_on"] == "ON"

def ac_is_off():
    return state["ac_on"] == "OFF"

def debounce_ok(rule):
    return (now_sec() - _last_trigger[rule]) > DEBOUNCE_SEC

def mark(rule):
    _last_trigger[rule] = now_sec()

def is_paused():
    return os.path.exists(PAUSE_FLAG)

def write_log(rule_id, rule_name, detail_lines, action):
    """
    Tulis ke file log DAN stdout.
    Format:
      [HH:MM:SS] A1 PAGI ON
        SOC=63%  AC=OFF
        → AC ON
    """
    ts = hms()
    header = f"[{ts}] {rule_id} {rule_name}"

    # Stdout (ditangkap journalctl)
    log.info(header)
    for line in detail_lines:
        log.info(f"  {line}")
    log.info(f"  → {action}")

    # File log (dibaca menu.py opsi 2)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{header}\n")
            for line in detail_lines:
                f.write(f"  {line}\n")
            f.write(f"  → {action}\n")
            f.write("\n")
    except Exception as e:
        log.warning(f"Gagal tulis log: {e}")

    # Last rule file (dibaca menu.py header RULE LAST)
    try:
        short_time = datetime.now().strftime("%H:%M")
        with open(LAST_RULE_FILE, "w") as f:
            f.write(f"{rule_id} {rule_name} {short_time}\n")
    except Exception as e:
        log.warning(f"Gagal tulis last_rule: {e}")

# ================================================================
# MQTT PUBLISH
# ================================================================
_client = None

def send_ac(value):
    """value: 'ON' atau 'OFF'"""
    if _client:
        _client.publish(TOPIC_CMD, value)
        log.info(f"  CMD → AC {value}")

# ================================================================
# 7 RULE AUTOMATION
# ================================================================
def check_rules():
    global _a4_timer_start, _a4_fired

    if is_paused():
        return

    soc    = state["soc"]
    pv     = state["pv"]
    ac_out = state["ac_out"]
    grid_v = state["grid_v"]

    # Minimal butuh SOC untuk bisa berjalan
    if soc is None:
        return

    # ────────────────────────────────────────────────────────────
    # A2 — SOC Rendah AC OFF
    # Proteksi baterai, MENANG atas semua (tidak ada guard PLN)
    # Threshold: SOC < 41, gap dengan A6 (>41) di SOC=40
    # ────────────────────────────────────────────────────────────
    if soc < 41 and ac_is_on() and debounce_ok("A2"):
        write_log(
            "A2", "SOC RENDAH",
            [f"SOC={soc:.0f}%  AC=ON"],
            "AC OFF (proteksi baterai)"
        )
        send_ac("OFF")
        mark("A2")
        # Reset A4 timer karena AC sudah off
        _a4_timer_start = None
        _a4_fired       = False
        return  # A2 menang, stop cek rule lain

    # ────────────────────────────────────────────────────────────
    # A6 — PLN Mati AC ON
    # SOC > 41 (gap: A2 menang di 40, A6 tidak aktif di 40)
    # ────────────────────────────────────────────────────────────
    if (grid_v is not None
            and grid_v < 50
            and soc > 41
            and ac_is_off()
            and debounce_ok("A6")):
        write_log(
            "A6", "PLN MATI",
            [f"GRID={grid_v:.0f}V  SOC={soc:.0f}%"],
            "AC ON (backup rumah)"
        )
        send_ac("ON")
        mark("A6")
        return

    # ────────────────────────────────────────────────────────────
    # A7 — PLN Hidup AC OFF (malam)
    # Hanya jalan malam + PLN kembali setelah outage (A6 pernah trigger)
    # ────────────────────────────────────────────────────────────
    if (grid_v is not None
            and grid_v > 180
            and ac_is_on()
            and is_nighttime()
            and _last_trigger["A6"] > 0
            and (now_sec() - _last_trigger["A6"]) < 3600
            and debounce_ok("A7")):
        write_log(
            "A7", "PLN HIDUP",
            [f"GRID={grid_v:.0f}V  malam"],
            "AC OFF"
        )
        send_ac("OFF")
        mark("A7")
        return

    # ────────────────────────────────────────────────────────────
    # A1 — Pagi AC ON (jam 09:00)
    # ────────────────────────────────────────────────────────────
    if (hour() == 9
            and soc >= 60
            and ac_is_off()
            and debounce_ok("A1")):
        write_log(
            "A1", "PAGI ON",
            [f"SOC={soc:.0f}%  AC=OFF  jam 09:xx"],
            "AC ON"
        )
        send_ac("ON")
        mark("A1")
        return

    # ────────────────────────────────────────────────────────────
    # A5 — Standby Malam AC OFF
    # DUA GUARD: (1) malam saja, (2) PLN ada (tidak saat outage)
    # Tanpa guard PLN → A5 melumpuhkan A6 saat PLN mati malam
    # ────────────────────────────────────────────────────────────
    if (is_nighttime()
            and soc < 61
            and ac_is_on()
            and grid_v is not None
            and grid_v > 180          # Guard PLN: hanya saat grid normal
            and debounce_ok("A5")):
        write_log(
            "A5", "STANDBY MALAM",
            [f"SOC={soc:.0f}%  GRID={grid_v:.0f}V  malam"],
            "AC OFF"
        )
        send_ac("OFF")
        mark("A5")
        return

    # ────────────────────────────────────────────────────────────
    # A3 — Recovery AC ON (siang, SOC pulih + solar cukup)
    # ────────────────────────────────────────────────────────────
    if (is_daytime()
            and soc >= 60
            and ac_is_off()
            and pv is not None
            and pv > 50
            and debounce_ok("A3")):
        write_log(
            "A3", "RECOVERY",
            [f"SOC={soc:.0f}%  PV={pv:.0f}W  AC=OFF"],
            "AC ON"
        )
        send_ac("ON")
        mark("A3")
        return

    # ────────────────────────────────────────────────────────────
    # A4 — Solar Lemah AC OFF (kondisi bertahan 5 menit)
    # Cek: PV < LOAD dan SOC <= 60 selama 5 menit berturutan
    # ────────────────────────────────────────────────────────────
    if (is_daytime()
            and ac_is_on()
            and pv is not None
            and ac_out is not None
            and pv < ac_out
            and soc <= 60):
        if _a4_timer_start is None:
            _a4_timer_start = now_sec()
            log.info(f"A4 timer mulai: PV={pv:.0f}W LOAD={ac_out:.0f}W SOC={soc:.0f}%")

        elapsed = now_sec() - _a4_timer_start
        if elapsed >= 300 and not _a4_fired and debounce_ok("A4"):
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            write_log(
                "A4", "SOLAR LEMAH",
                [f"PV={pv:.0f}W  LOAD={ac_out:.0f}W  SOC={soc:.0f}%",
                 f"durasi={mins}m{secs:02d}s"],
                "AC OFF"
            )
            send_ac("OFF")
            mark("A4")
            _a4_fired = True
    else:
        # Kondisi tidak terpenuhi → reset timer
        if _a4_timer_start is not None:
            log.info("A4 timer reset (kondisi tidak lagi terpenuhi)")
        _a4_timer_start = None
        _a4_fired       = False

# ================================================================
# MQTT CALLBACKS
# ================================================================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info(f"MQTT terhubung ke {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe([
            (TOPIC_SOC,    0),
            (TOPIC_PV,     0),
            (TOPIC_AC_OUT, 0),
            (TOPIC_GRID_V, 0),
            (TOPIC_AC_ON,  0),
        ])
        log.info(f"Subscribe: {DEVICE_NAME}")
    else:
        log.error(f"MQTT gagal konek rc={rc}")

def on_disconnect(client, userdata, rc):
    log.warning(f"MQTT terputus rc={rc}, reconnect otomatis...")

def on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode().strip()

    try:
        val = float(payload)
        if topic == TOPIC_SOC:
            state["soc"]    = val
        elif topic == TOPIC_PV:
            state["pv"]     = val
        elif topic == TOPIC_AC_OUT:
            state["ac_out"] = val
        elif topic == TOPIC_GRID_V:
            state["grid_v"] = val
    except ValueError:
        if topic == TOPIC_AC_ON:
            state["ac_on"] = payload.upper()

    # Cek rule setiap kali data masuk
    check_rules()

# ================================================================
# MAIN
# ================================================================
def main():
    global _client

    log.info("=" * 50)
    log.info("  BLUETTI AUTOMATION v1.0")
    log.info(f"  Device : {DEVICE_NAME}")
    log.info(f"  Broker : {MQTT_BROKER}:{MQTT_PORT}")
    log.info(f"  Log    : {LOG_FILE}")
    log.info("=" * 50)

    if is_paused():
        log.warning("PAUSED saat start (file flag ada). Resume via menu.py.")

    _client = mqtt.Client()
    _client.on_connect    = on_connect
    _client.on_message    = on_message
    _client.on_disconnect = on_disconnect

    _client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    _client.loop_forever()

if __name__ == "__main__":
    main()
