#!/usr/bin/env python3
import csv
import os
import time
from datetime import datetime
import paho.mqtt.client as mqtt

MQTT_BROKER  = "127.0.0.1"
MQTT_PORT    = 1883
DEVICE_NAME  = "PR200V2-2551110318791"

CSV_RAM      = "/tmp/bluetti_history.csv"        # tulis ke RAM
CSV_DISK     = os.path.expanduser("~/bluetti_history.csv")  # archive disk
LOG_INTERVAL = 60    # detik antar log
FLUSH_INTERVAL = 3600  # flush ke disk tiap 1 jam

TOPIC_SOC    = f"bluetti/state/{DEVICE_NAME}/total_battery_percent"
TOPIC_PV     = f"bluetti/state/{DEVICE_NAME}/dc_input_power"
TOPIC_AC_OUT = f"bluetti/state/{DEVICE_NAME}/ac_output_power"
TOPIC_GRID_V = f"bluetti/state/{DEVICE_NAME}/ac_input_voltage"
TOPIC_AC_ON  = f"bluetti/state/{DEVICE_NAME}/ac_output_on"

state = {"soc": None, "pv": None, "ac_out": None, "grid_v": None, "ac_on": None}
last_log_time   = 0
last_flush_time = 0

def ensure_header(path):
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(["timestamp","soc","pv","ac_out","grid_v","ac_on"])

def write_row(path, row):
    with open(path, "a", newline="") as f:
        csv.writer(f).writerow(row)

def flush_to_disk():
    """Salin CSV dari RAM ke disk."""
    if not os.path.exists(CSV_RAM):
        return
    ensure_header(CSV_DISK)
    with open(CSV_RAM, "r") as src:
        rows = list(csv.reader(src))
    # Skip header, append data saja
    data_rows = rows[1:] if rows and rows[0][0] == "timestamp" else rows
    if not data_rows:
        return
    with open(CSV_DISK, "a", newline="") as dst:
        w = csv.writer(dst)
        w.writerows(data_rows)
    # Reset RAM file (hanya header)
    ensure_header(CSV_RAM)
    # Hapus isi lama lalu tulis ulang header
    with open(CSV_RAM, "w", newline="") as f:
        csv.writer(f).writerow(["timestamp","soc","pv","ac_out","grid_v","ac_on"])
    print(f"[FLUSH] {len(data_rows)} baris → {CSV_DISK}")

def log_history():
    global last_log_time, last_flush_time
    now = time.time()
    if now - last_log_time < LOG_INTERVAL:
        return
    if any(v is None for v in state.values()):
        return
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        state["soc"], state["pv"], state["ac_out"],
        state["grid_v"], state["ac_on"],
    ]
    write_row(CSV_RAM, row)
    print(f"[LOG] SOC={state['soc']}% PV={state['pv']}W LOAD={state['ac_out']}W")
    last_log_time = now
    # Flush ke disk tiap jam
    if now - last_flush_time >= FLUSH_INTERVAL:
        flush_to_disk()
        last_flush_time = now

def on_connect(client, userdata, flags, rc):
    print("MQTT connected")
    client.subscribe([(TOPIC_SOC,0),(TOPIC_PV,0),(TOPIC_AC_OUT,0),(TOPIC_GRID_V,0),(TOPIC_AC_ON,0)])

def on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    try:
        val = float(payload)
        if   topic == TOPIC_SOC:    state["soc"]    = val
        elif topic == TOPIC_PV:     state["pv"]     = val
        elif topic == TOPIC_AC_OUT: state["ac_out"] = val
        elif topic == TOPIC_GRID_V: state["grid_v"] = val
    except ValueError:
        if topic == TOPIC_AC_ON:
            state["ac_on"] = payload.upper()
    log_history()

ensure_header(CSV_RAM)
ensure_header(CSV_DISK)

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_forever()
