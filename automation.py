#!/usr/bin/env python3
"""
automation.py — Bluetti Elite 200 V2 Automation
v3.1 - Smart Recovery Tiered, Exceptions Fixed
"""

import os
import time
import logging
from datetime import datetime
import paho.mqtt.client as mqtt

# ================================================================
# KONFIGURASI
# ================================================================
MQTT_BROKER  = "127.0.0.1"
MQTT_PORT    = 1883
DEVICE_NAME  = "PR200V2-2551110318791"

TOPIC_SOC    = f"bluetti/state/{DEVICE_NAME}/total_battery_percent"
TOPIC_PV     = f"bluetti/state/{DEVICE_NAME}/dc_input_power"
TOPIC_AC_OUT = f"bluetti/state/{DEVICE_NAME}/ac_output_power"
TOPIC_GRID_V = f"bluetti/state/{DEVICE_NAME}/ac_input_voltage"
TOPIC_AC_ON  = f"bluetti/state/{DEVICE_NAME}/ac_output_on"
TOPIC_CMD    = f"bluetti/command/{DEVICE_NAME}/ac_output_on"

LOG_FILE       = os.path.expanduser("~/bluetti_log.txt")
LAST_RULE_FILE = os.path.expanduser("~/bluetti_last_rule.txt")
PAUSE_FLAG     = "/tmp/automation_paused"

DEBOUNCE_SEC   = 120

# ================================================================
# LOGGING
# ================================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ================================================================
# STATE & TIMERS
# ================================================================
state = {
    "soc": None, "pv": None, "ac_out": None, "grid_v": None, "ac_on": None,
}

_last_trigger = {f"A{i}": 0.0 for i in ["1", "1b", "2", "3", "4_pagi", "4_siang", "5", "6", "7"]}
_timers = {"A2": None, "A4_siang": None, "A7": None}
_a6_active = False

def now_sec(): return time.time()
def ac_is_on(): return state["ac_on"] == "ON"
def ac_is_off(): return state["ac_on"] != "ON"
def debounce_ok(rule): return (now_sec() - _last_trigger[rule]) > DEBOUNCE_SEC
def is_paused(): return os.path.exists(PAUSE_FLAG)

def is_time_range(start_str, end_str):
    now_str = datetime.now().strftime("%H:%M")
    if start_str <= end_str:
        return start_str <= now_str < end_str
    return start_str <= now_str or now_str < end_str

def check_timer(rule, condition, duration_sec):
    if condition:
        if _timers[rule] is None:
            _timers[rule] = now_sec()
        if (now_sec() - _timers[rule]) >= duration_sec:
            return True
    else:
        _timers[rule] = None
    return False

def write_log(rule_id, rule_name, detail_lines, action):
    ts = datetime.now().strftime("%H:%M:%S")
    header = f"[{ts}] {rule_id} {rule_name}"
    log.info(header)
    for line in detail_lines: log.info(f"  {line}")
    log.info(f"  → {action}")
    
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{header}\n" + "\n".join([f"  {l}" for l in detail_lines]) + f"\n  → {action}\n\n")
        with open(LAST_RULE_FILE, "w") as f:
            f.write(f"{rule_id} {rule_name} {datetime.now().strftime('%H:%M')}\n")
    except Exception as e:
        log.warning(f"File log error: {e}")

_client = None
def send_ac(value):
    global state
    state["ac_on"] = value
    if _client:
        _client.publish(TOPIC_CMD, value)
        log.info(f"  CMD → AC {value}")

def trigger(rule_id, name, details, action, val):
    write_log(rule_id, name, details, action)
    send_ac(val)
    _last_trigger[rule_id] = now_sec()

# ================================================================
# EVALUASI RULE
# ================================================================
def check_rules():
    if is_paused(): return
    soc, pv, ac_out, grid = state["soc"], state["pv"], state["ac_out"], state["grid_v"]
    if soc is None: return

    is_malam = is_time_range("15:30", "06:00")

    # ────────────────────────────────────────────────────────────
    # 1. PROTEKSI ABSOLUT
    # ────────────────────────────────────────────────────────────
    if check_timer("A2", soc <= 40, 30):
        if ac_is_on() and debounce_ok("A2"):
            trigger("A2", "PROTEKSI BATERAI", [f"SOC={soc:.0f}% stabil 30s"], "AC OFF", "OFF")
        return

    # ────────────────────────────────────────────────────────────
    # 2. FASE MALAM / OUTAGE
    # ────────────────────────────────────────────────────────────
    if grid is not None and grid < 200 and is_malam and soc >= 41:
        if ac_is_off() and debounce_ok("A6"):
            global _a6_active
            _a6_active = True
            trigger("A6", "PLN MATI MALAM", [f"GRID={grid:.0f}V", f"SOC={soc:.0f}%"], "AC ON", "ON")
        return

    if check_timer("A7", grid is not None and grid >= 215 and is_malam and ac_is_on(), 30):
        if debounce_ok("A7"):
            global _a6_active
            _a6_active = False
            trigger("A7", "PLN HIDUP KEMBALI", [f"GRID={grid:.0f}V stabil 30s"], "AC OFF", "OFF")
        return

    if is_malam and soc < 61:
        if ac_is_on() and debounce_ok("A5") and not _a6_active:
            trigger("A5", "STANDBY MALAM", [f"SOC={soc:.0f}% < 61%"], "AC OFF", "OFF")
        return

    # ────────────────────────────────────────────────────────────
    # 3. KICKSTART PAGI
    # ────────────────────────────────────────────────────────────
    if is_time_range("06:00", "06:05") and soc >= 65:
        if ac_is_off() and debounce_ok("A1b"):
            trigger("A1b", "KICKSTART JAM 6", [f"SOC={soc:.0f}% (>= 65)"], "AC ON", "ON")
        return

    # Jam 7: Menunggu matahari naik, menyapu gap 46-64%
    if is_time_range("07:00", "07:05") and (45 < soc < 65):
        if ac_is_off() and debounce_ok("A1"):
            trigger("A1", "KICKSTART JAM 7", [f"SOC={soc:.0f}% (46 - 64)"], "AC ON", "ON")
        return

    # ────────────────────────────────────────────────────────────
    # 4. FASE SIANG: PEMUTUS (OFF)
    # ────────────────────────────────────────────────────────────
    if is_time_range("06:30", "11:30") and soc <= 45:
        if ac_is_on() and debounce_ok("A4_pagi"):
            trigger("A4_pagi", "BUFFER PAGI OFF", [f"SOC={soc:.0f}% <= 45%"], "AC OFF", "OFF")
        return

    if is_time_range("11:30", "15:30") and ac_is_on() and pv is not None and ac_out is not None:
        a4_siang_cond = (soc <= 60) and (pv < ac_out)
        if check_timer("A4_siang", a4_siang_cond, 900):
            if debounce_ok("A4_siang"):
                trigger("A4_siang", "SMART TRANSITION OFF", [f"SOC={soc:.0f}% <= 60%", f"PV={pv:.0f}W < LOAD={ac_out:.0f}W"], "AC OFF", "OFF")
            return
    else:
        check_timer("A4_siang", False, 900)

    # ────────────────────────────────────────────────────────────
    # 5. FASE SIANG: RECOVERY (ON)
    # ────────────────────────────────────────────────────────────
    if is_time_range("06:30", "15:30"):
        a3_cond = False
        if is_time_range("06:30", "11:30"):
            a3_cond = (soc > 65)
        else:
            # Tiered SOC: Syarat ketat jika mendung, bebas jika penuh
            a3_cond = (soc > 65 and pv is not None and pv > 200) or (soc > 75)
            
        if a3_cond:
            if ac_is_off() and debounce_ok("A3"):
                trigger("A3", "RECOVERY SIANG", [f"SOC={soc:.0f}%"], "AC ON", "ON")
            return

# ================================================================
# MQTT CALLBACKS
# ================================================================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe([(TOPIC_SOC,0), (TOPIC_PV,0), (TOPIC_AC_OUT,0), (TOPIC_GRID_V,0), (TOPIC_AC_ON,0)])
        log.info(f"Connected & Subscribed to {DEVICE_NAME}")

def on_message(client, userdata, msg):
    try:
        val = float(msg.payload.decode().strip())
        if msg.topic == TOPIC_SOC: state["soc"] = val
        elif msg.topic == TOPIC_PV: state["pv"] = val
        elif msg.topic == TOPIC_AC_OUT: state["ac_out"] = val
        elif msg.topic == TOPIC_GRID_V: state["grid_v"] = val
    except ValueError:
        if msg.topic == TOPIC_AC_ON: state["ac_on"] = msg.payload.decode().strip().upper()
    check_rules()

def main():
    global _client
    log.info("BLUETTI AUTOMATION v3.1 (Smart Recovery)")
    _client = mqtt.Client()
    _client.on_connect, _client.on_message = on_connect, on_message
    _client.connect(MQTT_BROKER, MQTT_PORT, 60)
    _client.loop_forever()

if __name__ == "__main__":
    main()
