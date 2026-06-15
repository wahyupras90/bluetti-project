#!/usr/bin/env python3
"""
chart_server.py — Bluetti Dashboard (2 tab: Control + Chart)
Port    : 8080
Service : chart.service
"""

import os, csv, json, re, subprocess, threading, urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import paho.mqtt.client as mqtt

CSV_FILE      = "/tmp/bluetti_history.csv"
CSV_DISK      = os.path.expanduser("~/bluetti_history.csv")
LOG_FILE      = os.path.expanduser("~/bluetti_log.txt")
LAST_RULE_FILE= os.path.expanduser("~/bluetti_last_rule.txt")
PAUSE_FLAG    = "/tmp/automation_paused"
DEVICE_NAME   = "PR200V2-2551110318791"
PORT          = 8080
STALE_SEC     = 90
TIME_BALANCE_W= 10

TOPIC_SOC    = f"bluetti/state/{DEVICE_NAME}/total_battery_percent"
TOPIC_PV     = f"bluetti/state/{DEVICE_NAME}/dc_input_power"
TOPIC_AC_OUT = f"bluetti/state/{DEVICE_NAME}/ac_output_power"
TOPIC_GRID_V = f"bluetti/state/{DEVICE_NAME}/ac_input_voltage"
TOPIC_AC_ON  = f"bluetti/state/{DEVICE_NAME}/ac_output_on"
TOPIC_CHG    = f"bluetti/state/{DEVICE_NAME}/total_battery_charge_time"
TOPIC_DCHG   = f"bluetti/state/{DEVICE_NAME}/total_battery_discharge_time"
TOPIC_CMD    = f"bluetti/command/{DEVICE_NAME}/ac_output_on"

RULE_COLORS = {
    "A1": "rgba(59,130,246,0.18)",   "A2": "rgba(220,38,38,0.18)",
    "A3": "rgba(22,163,74,0.18)",    "A4": "rgba(234,88,12,0.18)",
    "A5": "rgba(202,138,4,0.18)",    "A6": "rgba(147,51,234,0.18)",
    "A7": "rgba(107,114,128,0.18)",
}
RULE_LABELS = {
    "A1":"A1 Morning ON","A2":"A2 Low SOC","A3":"A3 Recovery",
    "A4":"A4 Weak Solar","A5":"A5 Standby Night","A6":"A6 Grid Down","A7":"A7 Grid Up",
}


def get_system_info():
    """Baca CPU, suhu, RAM, disk, uptime dari sistem."""
    info = {}
    try:
        # Suhu CPU
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            info["temp"] = round(int(f.read().strip()) / 1000, 1)
    except: info["temp"] = None

    try:
        # CPU usage (baca dua kali selisih 0.5 detik)
        import time
        def read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            return idle, total
        i1, t1 = read_cpu()
        time.sleep(0.5)
        i2, t2 = read_cpu()
        info["cpu"] = round((1 - (i2-i1)/(t2-t1)) * 100, 1)
    except: info["cpu"] = None

    try:
        # RAM
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.strip().split()[0])
        total_mb = mem["MemTotal"] // 1024
        avail_mb = mem["MemAvailable"] // 1024
        used_mb  = total_mb - avail_mb
        info["ram_used"] = used_mb
        info["ram_total"] = total_mb
        info["ram_pct"]  = round(used_mb / total_mb * 100)
    except: info["ram_used"] = info["ram_total"] = info["ram_pct"] = None

    try:
        # Disk
        import os
        st = os.statvfs(os.path.expanduser("~"))
        total_gb = round(st.f_blocks * st.f_frsize / 1e9, 1)
        free_gb  = round(st.f_bavail * st.f_frsize / 1e9, 1)
        used_gb  = round(total_gb - free_gb, 1)
        info["disk_used"]  = used_gb
        info["disk_total"] = total_gb
        info["disk_pct"]   = round(used_gb / total_gb * 100)
    except: info["disk_used"] = info["disk_total"] = info["disk_pct"] = None

    try:
        # Uptime
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        info["uptime"] = f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m"
    except: info["uptime"] = None

    return info

# ================================================================
# MQTT STATE
# ================================================================
state = {
    "soc":None,"pv":None,"ac_out":None,"grid_v":None,
    "ac_on":None,"chg_time":None,"last_ts":None,"mqtt_ok":False,
}
state_lock = threading.Lock()
_mqtt_client = None

def on_connect(client, userdata, flags, rc):
    with state_lock: state["mqtt_ok"] = (rc == 0)
    if rc == 0:
        client.subscribe([(t,0) for t in [
            TOPIC_SOC,TOPIC_PV,TOPIC_AC_OUT,TOPIC_GRID_V,
            TOPIC_AC_ON,TOPIC_CHG,TOPIC_DCHG,
            f"bluetti/state/{DEVICE_NAME}/dc_output_on",
            f"bluetti/state/{DEVICE_NAME}/dc_output_power"
        ]])

def on_disconnect(client, userdata, rc):
    with state_lock: state["mqtt_ok"] = False

def on_message(client, userdata, msg):
    t = msg.topic; p = msg.payload.decode().strip()
    with state_lock:
        try:
            v = float(p)
            if t==TOPIC_SOC:    state["soc"]      = v
            elif t==TOPIC_PV:   state["pv"]       = v
            elif t==TOPIC_AC_OUT: state["ac_out"] = v
            elif t==TOPIC_GRID_V: state["grid_v"] = v
            elif t in (TOPIC_CHG,TOPIC_DCHG): state["chg_time"] = int(v)
        except ValueError:
            if t==TOPIC_AC_ON: state["ac_on"] = p.upper()
            elif t.endswith("/dc_output_on"): state["dc_on"] = p.upper()
            elif t.endswith("/dc_output_power"):
                try: state["dc_out"] = float(p)
                except: pass
        state["last_ts"] = __import__("time").time()

def mqtt_thread():
    global _mqtt_client
    _mqtt_client = mqtt.Client()
    _mqtt_client.on_connect = on_connect
    _mqtt_client.on_message = on_message
    _mqtt_client.on_disconnect = on_disconnect
    import time
    while True:
        try:
            _mqtt_client.connect("127.0.0.1", 1883, 60)
            _mqtt_client.loop_forever()
        except Exception:
            time.sleep(5)

# ================================================================
# HELPERS
# ================================================================
def service_info(name):
    try:
        r = subprocess.run(["systemctl","is-active",name],capture_output=True,text=True,timeout=3)
        active = r.stdout.strip() == "active"
        uptime = ""
        if active:
            r2 = subprocess.run(["systemctl","show",name,"--property=ActiveEnterTimestamp"],
                                 capture_output=True,text=True,timeout=3)
            ts_str = r2.stdout.strip().split("=",1)[-1].strip()
            parts = ts_str.split()
            if len(parts)>=3:
                try:
                    started = datetime.strptime(f"{parts[1]} {parts[2]}","%Y-%m-%d %H:%M:%S")
                    d = datetime.now()-started
                    h,m = int(d.total_seconds()//3600), int((d.total_seconds()%3600)//60)
                    uptime = f"{h}h {m}m"
                except: pass
        return active, uptime
    except: return False, ""

def get_status():
    import time as _time
    with state_lock:
        s = dict(state)
    now = datetime.now().strftime("%H:%M:%S")
    last_ts = s["last_ts"]
    if last_ts:
        elapsed = _time.time() - last_ts
        fresh   = elapsed < STALE_SEC
        last_upd= f"{int(elapsed)}s ago" if elapsed < 60 else f"{int(elapsed//60)}m ago"
    else:
        fresh, last_upd = False, "never"

    bt_active, bt_uptime = service_info("bluetti")
    auto_active, _       = service_info("automation")
    paused = os.path.exists(PAUSE_FLAG)

    # TIME REM
    time_rem = "--"; time_rem_dir = 0
    if s["chg_time"] is not None and s["pv"] is not None and s["ac_out"] is not None:
        mins = s["chg_time"]
        h, m = mins//60, mins%60
        ts   = f"{h}h {m}m" if h>0 else f"{m}m"
        diff = s["pv"] - s["ac_out"]
        if diff > TIME_BALANCE_W:
            time_rem = f"{ts} ↑"; time_rem_dir = 1
        elif diff < -TIME_BALANCE_W:
            time_rem = f"{ts} ↓"; time_rem_dir = -1

    # Log terakhir
    last_rule = "--"
    try:
        if os.path.exists(LAST_RULE_FILE):
            with open(LAST_RULE_FILE) as f: last_rule = f.read().strip() or "--"
    except: pass

    # Log tail
    log_lines = []
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE) as f: lines = f.readlines()
            log_lines = [l.rstrip() for l in lines[-60:]]
    except: pass

    return {
        "time": now,
        "soc":  s["soc"], "pv": s["pv"], "ac_out": s["ac_out"],
        "grid_v": s["grid_v"], "ac_on": s["ac_on"], "dc_on": s.get("dc_on","OFF"), "dc_out": s.get("dc_out",0),
        "time_rem": time_rem, "time_rem_dir": time_rem_dir,
        "mqtt_ok": s["mqtt_ok"], "fresh": fresh, "last_upd": last_upd,
        "bt_active": bt_active, "bt_uptime": bt_uptime,
        "auto_active": auto_active, "auto_paused": paused,
        "last_rule": last_rule,
        "log": log_lines,
        "est_a2": calc_est_a2(),
        "weather": get_weather(),
        "forecast_irr": (get_weather() or {}).get("irr_next" if datetime.now().minute >= 30 else "irr_now"),
    }


LAT = -7.884277
LON = 110.311251
_weather_cache = {"data": None, "ts": 0}
WEATHER_CACHE_SEC = 1800

def get_weather():
    import time as _time
    now = _time.time()
    if _weather_cache["data"] and (now - _weather_cache["ts"]) < WEATHER_CACHE_SEC:
        return _weather_cache["data"]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={LAT}&longitude={LON}"
               f"&daily=weathercode,shortwave_radiation_sum"
               f"&hourly=shortwave_radiation,weathercode"
               f"&timezone=Asia%2FJakarta&forecast_days=2")
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
        daily = d.get("daily", {})
        codes = daily.get("weathercode", [None, None])
        rad   = daily.get("shortwave_radiation_sum", [None, None])
        def wcode_to_icon(c):
            if c is None: return "?"
            if c == 0:    return "☀️"
            if c <= 2:    return "🌤️"
            if c <= 48:   return "⛅"
            if c <= 67:   return "🌧️"
            return "⛈️"
        def rad_to_kwh(r_val):
            if r_val is None: return None
            wh_m2 = r_val * 277.78
            est_wh = wh_m2 * 6 * 0.15
            return round(est_wh / 1000, 1)
        # Ambil irr jam sekarang dan +1 jam
        hourly_times = d.get("hourly", {}).get("time", [])
        hourly_irr   = d.get("hourly", {}).get("shortwave_radiation", [])
        hourly_wcode = d.get("hourly", {}).get("weathercode", [])
        from datetime import datetime as _dt
        now_str  = _dt.now().strftime("%Y-%m-%dT%H:00")
        nxt_str  = _dt.now().replace(minute=0,second=0,microsecond=0)
        from datetime import timedelta as _td
        nxt_str  = (_dt.now().replace(minute=0,second=0,microsecond=0) + _td(hours=1)).strftime("%Y-%m-%dT%H:00")
        irr_now  = hourly_irr[hourly_times.index(now_str)]  if now_str in hourly_times else None
        irr_next = hourly_irr[hourly_times.index(nxt_str)]  if nxt_str in hourly_times else None

        # Bangun irr_today dan irr_tomorrow per jam
        from datetime import datetime as _dt2
        today_str = _dt2.now().strftime("%Y-%m-%d")
        tomorrow_str = (_dt2.now() + __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")

        def wcode_for_hour(t_str):
            # Ambil weathercode harian sebagai icon per jam (simplifikasi)
            return wcode_to_icon(codes[0] if codes else None)

        irr_today = []
        irr_tomorrow = []
        for idx2, t in enumerate(hourly_times):
            if idx2 >= len(hourly_irr): break
            irr_val = round(hourly_irr[idx2], 1)
            h = int(t[11:13])
            h_icon = wcode_to_icon(hourly_wcode[idx2] if idx2 < len(hourly_wcode) else None)
            if t.startswith(today_str):
                irr_today.append({"h": h, "irr": irr_val, "icon": h_icon})
            elif t.startswith(tomorrow_str):
                irr_tomorrow.append({"h": h, "irr": irr_val, "icon": h_icon})

        result = {
            "today":    {"icon": wcode_to_icon(codes[0] if codes else None),    "pv_est": rad_to_kwh(rad[0] if rad else None)},
            "tomorrow": {"icon": wcode_to_icon(codes[1] if len(codes)>1 else None), "pv_est": rad_to_kwh(rad[1] if len(rad)>1 else None)},
            "irr_now": irr_now, "irr_next": irr_next,
            "irr_today": irr_today, "irr_tomorrow": irr_tomorrow,
        }
        _weather_cache["data"] = result
        _weather_cache["ts"]   = now
        return result
    except:
        return None

def calc_est_a2():
    if not os.path.exists(CSV_FILE): return None
    try:
        cutoff = datetime.now() - timedelta(minutes=10)
        rows = []
        with open(CSV_FILE) as f:
            for row in csv.DictReader(f):
                try:
                    ts  = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    soc = float(row["soc"]) if row.get("soc") else None
                    if ts >= cutoff and soc is not None:
                        rows.append((ts, soc))
                except: continue
        if len(rows) < 2: return None
        rate = (rows[0][1] - rows[-1][1]) / ((rows[-1][0] - rows[0][0]).total_seconds() / 60)
        if rate <= 0: return None
        mins = (rows[-1][1] - 40) / rate
        return max(0, int(mins))
    except:
        return None

# ================================================================
# CHART DATA
# ================================================================
def load_csv(hours):
    # Baca dari disk (history lama) + RAM (data terbaru), merge dan deduplikasi
    import csv as _csv
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.now() - _td(hours=hours)
    seen = set()
    rows = []
    for path in [CSV_DISK, CSV_FILE]:
        if not os.path.exists(path): continue
        try:
            with open(path) as f:
                for row in _csv.DictReader(f):
                    try:
                        ts = _dt.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                        if ts < cutoff: continue
                        key = row["timestamp"]
                        if key in seen: continue
                        seen.add(key)
                        rows.append(row)
                    except: continue
        except: continue
    rows.sort(key=lambda r: r["timestamp"])
    if not rows: return []
    # Konversi ke format yang diharapkan get_chart
    result = []
    for row in rows:
        try:
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            label_ts = ts.strftime("%H:%M") if hours <= 24 else ts.strftime("%d/%m %H:%M")
            result.append({
                "ts":     label_ts,
                "soc":    float(row["soc"])    if row.get("soc")    else None,
                "pv":     float(row["pv"])     if row.get("pv")     else None,
                "ac_out": float(row["ac_out"]) if row.get("ac_out") else None,
            })
        except: continue
    return result

def load_rules(hours):
    if not os.path.exists(LOG_FILE): return []
    cutoff = datetime.now() - timedelta(hours=hours)
    events = []; today = datetime.now().strftime("%Y-%m-%d")
    pat = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]\s+(A\d+)\s+(.+)$')
    try:
        with open(LOG_FILE) as f:
            for line in f:
                m = pat.match(line.rstrip())
                if not m: continue
                ts_str, rule_id, _ = m.groups()
                try:
                    ts = datetime.strptime(f"{today} {ts_str}","%Y-%m-%d %H:%M:%S")
                    if ts < cutoff: continue
                    events.append({
                        "rule": rule_id,
                        "label": RULE_LABELS.get(rule_id, rule_id),
                        "time_str": ts_str[:5],
                        "color": RULE_COLORS.get(rule_id,"rgba(255,255,255,0.1)"),
                    })
                except: continue
    except: pass
    return events

def calc_summary(rows):
    pv   = sum(r["pv"]     for r in rows if r["pv"]     is not None)/60/1000
    load = sum(r["ac_out"] for r in rows if r["ac_out"] is not None)/60/1000
    return {"pv":round(pv,3),"load":round(load,3),"diff":round(pv-load,3)}


def load_degradation():
    """Baca bluetti_degradation.csv dan hitung ringkasan."""
    deg_file = os.path.expanduser("~/bluetti_degradation.csv")
    if not os.path.exists(deg_file):
        return None
    try:
        rows = []
        with open(deg_file) as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({
                        "date": row["date"],
                        "time": row["time"],
                        "eff_wh": float(row["eff_capacity_wh"]),
                    })
                except: continue
        if not rows:
            return None
        baseline  = rows[0]["eff_wh"]
        last      = rows[-1]
        deg_pct   = (1 - last["eff_wh"] / baseline) * 100 if baseline else 0
        return {
            "last_wh":   round(last["eff_wh"]),
            "last_date": f"{last['date']} {last['time']}",
            "baseline":  round(baseline),
            "deg_pct":   round(deg_pct, 1),
            "count":     len(rows),
        }
    except:
        return None

def get_chart(hours, label):
    rows  = load_csv(hours)
    rules = load_rules(hours)
    s     = calc_summary(rows)
    return {
        "labels": [r["ts"] for r in rows],
        "soc":    [r["soc"] for r in rows],
        "pv":     [r["pv"]  for r in rows],
        "ac_out": [r["ac_out"] for r in rows],
        "rules":  rules, "summary": s,
        "period": label, "count": len(rows),
        "degradation": load_degradation(),
    }

# ================================================================
# HTML
# ================================================================
HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bluetti</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hammer.js/2.0.8/hammer.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.0.1/chartjs-plugin-zoom.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'JetBrains Mono',monospace;min-height:100vh}

/* TAB NAV */
.tab-nav{display:flex;border-bottom:1px solid #1e293b;background:#0f172a;position:sticky;top:0;z-index:10}
.tab-btn{flex:1;padding:12px 0;font-family:'JetBrains Mono',monospace;font-size:13px;
  background:none;border:none;color:#475569;cursor:pointer;letter-spacing:1px;
  border-bottom:2px solid transparent;transition:all 0.15s}
.tab-btn.active{color:#e2e8f0;border-bottom-color:#0ea5e9}

.tab-content{display:none;padding:14px}
.tab-content.active{display:block}

/* STATUS */
.status-card{background:#1e293b;border-radius:8px;padding:14px;margin-bottom:10px}
.status-row{display:flex;justify-content:space-between;align-items:center;
  padding:6px 0;border-bottom:1px solid #0f172a;font-size:13px}
.status-row:last-child{border-bottom:none}
.status-label{color:#ffffff;font-size:14px;font-weight:700;letter-spacing:1px}
.status-value{font-weight:bold}
.soc-bar{height:4px;background:#0f172a;border-radius:2px;margin-top:4px;overflow:hidden}
.soc-fill{height:100%;border-radius:2px;transition:width 0.5s}

/* TOMBOL */
.btn-ac{width:100%;padding:14px;border-radius:8px;border:none;
  font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:bold;
  cursor:pointer;margin-bottom:8px;letter-spacing:1px;transition:all 0.15s}
.btn-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;width:100%}
.btn-grid-item{padding:13px 6px;border-radius:8px;border:none;font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:700;cursor:pointer;transition:all 0.15s}
.btn-ac-grid{background:#1a3a1a;color:#4ade80;border:1px solid #16a34a}
.btn-auto-grid{background:#1e293b;color:#94a3b8;border:1px solid #334155}
.btn-sys-grid{background:#1e3a5f;color:#e0f2fe;border:1px solid #0ea5e9}
.btn-log-grid{background:#1e293b;color:#94a3b8;border:1px solid #334155}
.btn-pause{width:100%;padding:11px;border-radius:8px;border:1px solid #334155;
  background:#1e293b;color:#94a3b8;font-family:'JetBrains Mono',monospace;
  font-size:14px;font-weight:700;cursor:pointer;margin-bottom:6px;transition:all 0.15s}
.btn-pause:hover{border-color:#0ea5e9;color:#e0f2fe}

/* LOG */
.log-box{background:#020617;border-radius:8px;padding:12px;margin-top:10px;
  max-height:200px;overflow-y:auto;font-size:11px;line-height:1.7}
.log-ts{color:#0ea5e9}
.log-action{color:#22c55e}
.log-detail{color:#64748b}

/* SYSTEM POPUP */
.sys-btn{width:100%;padding:11px;border-radius:8px;border:1px solid #334155;
  background:#1e293b;color:#94a3b8;font-family:'JetBrains Mono',monospace;
  font-size:14px;font-weight:700;cursor:pointer;margin-bottom:6px;letter-spacing:1px}
.sys-btn:hover{border-color:#0ea5e9;color:#e0f2fe}
.sys-row{display:flex;justify-content:space-between;padding:6px 0;
  border-bottom:1px solid #0f172a;font-size:13px}
.sys-row:last-child{border-bottom:none}
.sys-label{color:#94a3b8}
.reboot-btn{width:100%;padding:11px;border-radius:8px;border:none;
  background:#7f1d1d;color:#fca5a5;font-family:'JetBrains Mono',monospace;
  font-size:13px;font-weight:bold;cursor:pointer;margin-top:12px}
.reboot-btn:hover{background:#991b1b}

/* MODAL */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);
  z-index:100;align-items:center;justify-content:center}
.modal-bg.show{display:flex}
.modal{background:#1e293b;border-radius:12px;padding:20px;width:85%;max-width:320px;
  border:1px solid #334155}
.modal-title{font-size:13px;color:#94a3b8;margin-bottom:12px;letter-spacing:1px}
.modal-info{font-size:14px;margin-bottom:16px;color:#e2e8f0}
.modal-btns{display:flex;gap:8px}
.modal-cancel{flex:1;padding:10px;border-radius:6px;border:1px solid #334155;
  background:#0f172a;color:#94a3b8;font-family:'JetBrains Mono',monospace;cursor:pointer}
.modal-confirm{flex:1;padding:10px;border-radius:6px;border:none;
  font-family:'JetBrains Mono',monospace;font-weight:bold;cursor:pointer}

/* CHART TAB */
.period-row{display:flex;gap:8px;margin-bottom:12px}
.btn-period{flex:1;padding:8px 0;border:1px solid #334155;background:#1e293b;
  color:#94a3b8;border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:13px;cursor:pointer}
.btn-period.active{background:#0f4c75;border-color:#0ea5e9;color:#e0f2fe;font-weight:bold}
.filter-row{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.filter-item{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px}
.filter-box{width:14px;height:14px;border-radius:3px;border:2px solid;
  display:flex;align-items:center;justify-content:center;font-size:10px}
.filter-box.checked::after{content:'✓';font-weight:bold}
.chart-wrap{background:#1e293b;border-radius:8px;padding:14px;margin-bottom:10px}
.zoom-hint{font-size:10px;color:#475569;text-align:right;margin-bottom:6px}
.summary{background:#1e293b;border-radius:8px;padding:14px 16px}
.summary-title{font-size:11px;color:#64748b;letter-spacing:2px;margin-bottom:12px}
.summary-row{display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;border-bottom:1px solid #0f172a;font-size:13px}
.summary-row:last-child{border-bottom:none}
.summary-label{color:#64748b}
.summary-value{font-weight:bold}
.surplus{color:#22c55e}.deficit{color:#ef4444}
.divider{border:none;border-top:1px solid #0f172a;margin:10px 0}
.health-card{background:#1e293b;border-radius:8px;padding:14px 16px;margin-top:10px}
.health-title{font-size:11px;color:#94a3b8;letter-spacing:2px;margin-bottom:12px}
.health-row{display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;border-bottom:1px solid #0f172a;font-size:13px}
.health-row:last-child{border-bottom:none}
.health-label{color:#ffffff}
.health-note{font-size:10px;color:#94a3b8;margin-top:8px;line-height:1.5}
.reset-btn{display:block;width:100%;margin-top:10px;padding:8px;
  background:#1e293b;border:1px solid #334155;color:#94a3b8;
  border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:12px;cursor:pointer}

/* COLORS */
.green{color:#22c55e}.red{color:#ef4444}.yellow{color:#eab308}
.dim{color:#475569}.cyan{color:#0ea5e9}
</style>
</head>
<body>

<!-- TAB NAV -->
<div class="tab-nav">
  <button class="tab-btn active" onclick="switchTab('control',this)">⚡ CONTROL</button>
  <button class="tab-btn" onclick="switchTab('chart',this)">📊 CHART</button>
</div>

<!-- ══════════════ TAB CONTROL ══════════════ -->
<div id="tab-control" class="tab-content active">

  <!-- STATUS -->
  <!-- WEATHER -->
  <div id="weather-card" style="display:none;margin-bottom:10px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px;text-align:center;cursor:pointer" onclick="showForecast('today')">
        <div style="font-size:22px;margin-bottom:4px" id="w-today-icon">--</div>
        <div style="font-size:10px;color:#64748b;font-weight:700;letter-spacing:1px;margin-bottom:4px">TODAY</div>
        <div style="font-size:13px;font-weight:700;color:#eab308" id="w-today">--</div>
      </div>
      <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px;text-align:center;cursor:pointer" onclick="showForecast('tomorrow')">
        <div style="font-size:22px;margin-bottom:4px" id="w-tmr-icon">--</div>
        <div style="font-size:10px;color:#64748b;font-weight:700;letter-spacing:1px;margin-bottom:4px">TOMORROW</div>
        <div style="font-size:13px;font-weight:700;color:#eab308" id="w-tomorrow">--</div>
      </div>
    </div>
  </div>

  <div class="status-card" id="status-card">
    <div class="status-row">
      <span class="status-label">TIME</span>
      <span class="status-value cyan" id="s-time">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">SOC</span>
      <div style="text-align:right">
        <span class="status-value" id="s-soc">--%</span>
        <div class="soc-bar"><div class="soc-fill" id="s-soc-bar" style="width:0%"></div></div>
      </div>
    </div>
    <div class="status-row">
      <span class="status-label">BAT TREND</span>
      <span class="status-value" id="s-bat-trend">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">TIME REM</span>
      <span class="status-value" id="s-timerem">--</span>
    </div>

    <div class="status-row">
      <span class="status-label">PV</span>
      <span class="status-value" id="s-pv">--W</span>
    </div>
    <div class="status-row">
      <span class="status-label">SOLAR EFF</span>
      <span class="status-value" id="s-solar-eff">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">LOAD</span>
      <span class="status-value" id="s-load">--W</span>
    </div>
    <div class="status-row">
      <span class="status-label">NET</span>
      <span class="status-value" id="s-net">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">GRID</span>
      <span class="status-value" id="s-grid">--V</span>
    </div>
    <div class="status-row">
      <span class="status-label">AC</span>
      <span class="status-value" id="s-ac">--</span>
    </div>

    <div class="status-row">
      <span class="status-label">UPDATE</span>
      <span class="status-value" id="s-update">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">BLUETTI CONN</span>
      <span class="status-value" id="s-bt">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">LAST RULE</span>
      <span class="status-value dim" id="s-rule">--</span>
    </div>
  </div>

  <!-- 2x2 GRID TOMBOL -->
  <div class="btn-grid">
    <button class="btn-grid-item btn-ac-grid" id="btn-ac" onclick="const on=document.getElementById('s-ac').textContent==='ON';if(confirm(on?'Turn AC OFF?':'Turn AC ON?'))fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:'ac',value:on?'OFF':'ON'})}).then(()=>setTimeout(fetchStatus,1000))">--</button>
    <button class="btn-grid-item btn-auto-grid" id="btn-auto" onclick="const p=window._autoPaused;if(confirm(p?'Resume automation?':'Pause automation?'))fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:p?'resume':'pause'})}).then(()=>setTimeout(fetchStatus,1000))">⏸ Pause</button>
    <button class="btn-grid-item btn-sys-grid" onclick="showSystemPopup()">🖥️ System</button>
    <button class="btn-grid-item btn-log-grid" onclick="toggleLog()">📋 Log</button>
  </div>


</div>

<!-- ══════════════ TAB CHART ══════════════ -->
<div id="tab-chart" class="tab-content">

  <div id="flow-view">
    <canvas id="flowCv" style="display:block;width:100%;background:#0f172a;border-radius:12px"></canvas>
    <button onclick="window.showGraphView()" style="display:block;width:100%;padding:12px;margin-top:10px;border-radius:8px;border:1px solid #0ea5e9;background:#0f4c75;color:#e0f2fe;font-family:Courier New,monospace;font-size:13px;font-weight:bold;cursor:pointer">📊 Graph &amp; History</button>
  </div>

  <div id="graph-view" style="display:none">
    <button onclick="window.showFlowView()" style="display:block;width:100%;padding:11px;margin-bottom:12px;border-radius:8px;border:1px solid #334155;background:#1e293b;color:#94a3b8;font-family:Courier New,monospace;font-size:13px;cursor:pointer">← Back to Energy Flow</button>
  <div class="period-row">
    <button class="btn-period" onclick="loadChart(1,'1H',this)">1H</button>
    <button class="btn-period active" onclick="loadChart(24,'1D',this)">1D</button>
    <button class="btn-period" onclick="loadChart(168,'7D',this)">7D</button>
    <button class="btn-period" onclick="loadChart(720,'1M',this)">1M</button>
  </div>

  <div class="filter-row">
    <label class="filter-item" onclick="toggleFilter('soc')">
      <div class="filter-box checked" id="box-soc" style="border-color:#3b82f6;color:#3b82f6;background:#1e3a5f"></div>
      <span style="color:#3b82f6">SOC %</span>
    </label>
    <label class="filter-item" onclick="toggleFilter('pv')">
      <div class="filter-box checked" id="box-pv" style="border-color:#eab308;color:#eab308;background:#1a1a00"></div>
      <span style="color:#eab308">PV W</span>
    </label>
    <label class="filter-item" onclick="toggleFilter('acout')">
      <div class="filter-box checked" id="box-acout" style="border-color:#ef4444;color:#ef4444;background:#2d0000"></div>
      <span style="color:#ef4444">AC OUT W</span>
    </label>
  </div>

  <div class="chart-wrap">
    <div class="zoom-hint">pinch = zoom · drag = geser</div>
    <canvas id="mainChart" height="260"></canvas>
  </div>

  <div class="summary">
    <div class="summary-title" id="sum-title">ENERGY SUMMARY — 1D</div>
    <div class="summary-row">
      <span class="summary-label">☀️  PV generated</span>
      <span class="summary-value" id="sum-pv">—</span>
    </div>
    <div class="summary-row">
      <span class="summary-label">⚡  AC consumption</span>
      <span class="summary-value" id="sum-load">—</span>
    </div>
    <hr class="divider">
    <div class="summary-row">
      <span class="summary-label">📊  Difference</span>
      <span class="summary-value" id="sum-diff">—</span>
    </div>
  </div>

  <button class="reset-btn" onclick="resetZoom()">↺ Reset zoom</button>

  <div class="health-card">
    <div class="health-title">🔋 BATTERY HEALTH</div>
    <div id="health-content">
      <div class="health-note">Loading data...</div>
    </div>
  </div>

</div>
</div>

<!-- LOG MODAL -->
<div class="modal-bg" id="log-modal">
  <div class="modal" style="max-height:80vh;overflow-y:auto;width:92%">
    <div class="modal-title">📋 LOG</div>
    <div class="log-box" id="log-box" style="max-height:60vh;overflow-y:auto;margin:10px 0">
      <span class="dim">Loading log...</span>
    </div>
    <button class="modal-cancel" style="width:100%;margin-top:8px" onclick="closeLogModal()">Close</button>
  </div>
</div>

<!-- FORECAST MODAL -->
<div class="modal-bg" id="fc-modal">
  <div class="modal" style="border-radius:16px 16px 0 0;max-height:70vh;overflow-y:auto;padding:16px">
    <div class="modal-title" id="fc-title" style="text-align:center;margin-bottom:12px">FORECAST</div>
    <div id="fc-content"></div>
    <button class="modal-cancel" style="width:100%;margin-top:10px" onclick="document.getElementById('fc-modal').classList.remove('show')">Close</button>
  </div>
</div>

<!-- ══════════════ MODAL ══════════════ -->
<div class="modal-bg" id="modal">
  <div class="modal">
    <div class="modal-title" id="modal-title">KONFIRMASI</div>
    <div class="modal-info" id="modal-info"></div>
    <div class="modal-btns">
      <button class="modal-cancel" onclick="closeModal()">Batal</button>
      <button class="modal-confirm" id="modal-confirm" onclick="doConfirm()">Ya</button>
    </div>
  </div>
</div>

<!-- SYSTEM POPUP -->
<div class="modal-bg" id="sys-modal">
  <div class="modal">
    <div class="modal-title">🖥️ SYSTEM STATUS</div>
    <div id="sys-content">
      <div class="sys-row"><span class="sys-label">CPU</span><span id="sys-cpu">--</span></div>
      <div class="sys-row"><span class="sys-label">TEMP</span><span id="sys-temp">--</span></div>
      <div class="sys-row"><span class="sys-label">RAM</span><span id="sys-ram">--</span></div>
      <div class="sys-row"><span class="sys-label">DISK</span><span id="sys-disk">--</span></div>
      <div class="sys-row"><span class="sys-label">UPTIME</span><span id="sys-uptime">--</span></div>
    </div>
    <button class="reboot-btn" onclick="confirmReboot()">⟳ Reboot Pi</button>
    <div class="modal-btns" style="margin-top:8px">
      <button class="modal-cancel" style="flex:unset;width:100%" onclick="closeSysModal()">Close</button>
    </div>
  </div>
</div>

<script>
// ── TAB ──────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if (name === 'chart') { if(!window._fOK){window.initFlow();window._fOK=true;} }
}

// ── STATUS FETCH ─────────────────────────────────────────────────
let currentAcOn = '--';
let pendingAction = null;

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    applyStatus(d);
  } catch(e) {}
}

function applyStatus(d) {
  currentAcOn = d.ac_on || '--';

  // TIME
  document.getElementById('s-time').textContent = d.time;

  // SOC
  const soc = d.soc !== null ? d.soc : null;
  const socEl = document.getElementById('s-soc');
  const barEl = document.getElementById('s-soc-bar');
  if (soc !== null) {
    socEl.textContent = `${soc}%`;
    socEl.className = 'status-value ' + (soc>50?'green':soc>30?'yellow':'red');
    barEl.style.width = `${soc}%`;
    barEl.style.background = soc>50?'#22c55e':soc>30?'#eab308':'#ef4444';
  } else { socEl.textContent = '--%'; }

  // TIME REM
  const trEl = document.getElementById('s-timerem');
  if (d.time_rem && d.time_rem !== '--') {
    trEl.textContent = d.time_rem;
    trEl.className = 'status-value ' + (d.time_rem_dir>0?'green':d.time_rem_dir<0?'red':'dim');
  } else { trEl.textContent = '--'; trEl.className='status-value dim'; }

  // PV, LOAD
  document.getElementById('s-pv').textContent   = d.pv   !== null ? `${d.pv}W`    : '--W';
  document.getElementById('s-load').textContent = d.ac_out !== null ? `${d.ac_out}W` : '--W';

  // NET = PV - LOAD
  const netEl = document.getElementById('s-net');
  if (d.pv !== null && d.ac_out !== null) {
    const net = Math.round(d.pv - d.ac_out);
    netEl.textContent = (net >= 0 ? '+' : '') + net + 'W';
    netEl.className = 'status-value ' + (net > 0 ? 'green' : net < 0 ? 'red' : '');
  } else { netEl.textContent = '--'; netEl.className = 'status-value'; }

  // SOLAR EFF = PV actual vs forecast
  const sefEl = document.getElementById('s-solar-eff');
  if (d.forecast_irr && d.forecast_irr > 0 && d.pv !== null && d.pv > 0) {
    const eff = Math.round((d.pv / (d.forecast_irr * 0.715)) * 100);
    sefEl.textContent = eff + '% of fcst';
    sefEl.className = 'status-value ' + (eff >= 70 ? 'green' : eff >= 40 ? 'yellow' : 'red');
  } else { sefEl.textContent = '--'; sefEl.className = 'status-value dim'; }

  // BAT TREND
  if (window._lastSoc !== undefined && window._lastSocTime) {
    const dt = (Date.now() - window._lastSocTime) / 60000;
    if (dt > 0.5 && dt < 10) {
      const trend = (d.soc - window._lastSoc) / dt;
      const tStr = (trend >= 0 ? '↑ +' : '↓ ') + Math.abs(trend).toFixed(2) + '%/min';
      const tEl = document.getElementById('s-bat-trend');
      tEl.textContent = tStr;
      tEl.className = 'status-value ' + (trend > 0.05 ? 'green' : trend < -0.05 ? 'red' : 'dim');
    }
  }
  if (window._lastSoc !== d.soc) {
    window._lastSoc = d.soc;
    window._lastSocTime = Date.now();
  }

  // GRID
  const gv = d.grid_v;
  const gEl = document.getElementById('s-grid');
  if (gv !== null) {
    gEl.textContent = `${gv}V`;
    gEl.className = 'status-value ' + (gv>=200?'green':gv>=50?'yellow':'red');
  } else { gEl.textContent = '--V'; gEl.className='status-value dim'; }

  // AC
  const acEl = document.getElementById('s-ac');
  acEl.textContent = d.ac_on || '--';
  acEl.className = 'status-value ' + (d.ac_on==='ON'?'green':d.ac_on==='OFF'?'red':'dim');

  // UPDATE gabungan
  const updEl = document.getElementById('s-update');
  updEl.innerHTML = d.fresh
    ? `<span class="green">FRESH</span> · ${d.last_upd}`
    : `<span class="red">STALE</span> · ${d.last_upd}`;

  // BLUETTI CONN
  const btEl = document.getElementById('s-bt');
  btEl.textContent = d.bt_active ? `ACTIVE (${d.bt_uptime})` : 'INACTIVE';
  btEl.className = 'status-value ' + (d.bt_active?'green':'red');

  // LAST RULE + auto status
  const ruleEl = document.getElementById('s-rule');
  const autoStatus = d.auto_paused ? 'PAUSED' : d.auto_active ? 'ON' : 'OFF';
  // Format: "A4 MORN OFF · 10:08 · ON"
  const ruleRaw = d.last_rule || '--';
  const ruleParts = ruleRaw.split(' ');
  const ruleId   = ruleParts[0] || '';
  const ruleName = ruleParts[1] || '';
  const ruleTime = ruleParts[2] || '';
  const ruleShort = ruleId && ruleName ? ruleId + ' ' + ruleName + ' · ' + ruleTime + ' · AUTO ' + autoStatus : '--';
  ruleEl.textContent = ruleShort;
  ruleEl.className = 'status-value dim';
  window._autoPaused = d.auto_paused;
  const ab=document.getElementById('btn-auto');
  if(ab) ab.textContent=d.auto_paused?'▶ Resume Automation':'⏸ Pause Automation';



  // WEATHER
  const wCard = document.getElementById('weather-card');
  if (d.weather) {
    wCard.style.display = '';
    const w = d.weather;
    document.getElementById('w-today-icon').textContent = w.today.icon || '--';
    document.getElementById('w-today').textContent = w.today.pv_est !== null ? 'est. ~'+w.today.pv_est+' kWh' : '--';
    document.getElementById('w-tmr-icon').textContent = w.tomorrow.icon || '--';
    document.getElementById('w-tomorrow').textContent = w.tomorrow.pv_est !== null ? 'est. ~'+w.tomorrow.pv_est+' kWh' : '--';
    window._weatherData = w;
  } else {
    wCard.style.display = 'none';
  }

  // AC BUTTON
  const btn = document.getElementById('btn-ac');
  if (d.ac_on === 'ON') {
    btn.textContent = 'Turn AC OFF';
    btn.style.cssText = 'background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b';
  } else if (d.ac_on === 'OFF') {
    btn.textContent = 'Turn AC ON';
    btn.style.cssText = 'background:#14532d;color:#86efac;border:1px solid #166534';
  } else {
    btn.textContent = 'AC --';
    btn.style.cssText = 'background:#1e293b;color:#475569;border:1px solid #334155';
  }

  // LOG
  if (d.log && d.log.length) {
    const logBox = document.getElementById('log-box');
    logBox.innerHTML = d.log.map(line => {
      if (line.startsWith('['))
        return `<div><span class="log-ts">${line.substring(0,10)}</span><span style="color:#e2e8f0">${line.substring(10)}</span></div>`;
      if (line.trim().startsWith('→'))
        return `<div class="log-action">${line}</div>`;
      return `<div class="log-detail">${line}</div>`;
    }).join('');
    logBox.scrollTop = logBox.scrollHeight;
  }
}

// Auto-refresh 10 detik

(function(){
var cv,ctx,fd={},raf=null,pts=[],W=0,H=0;

window.showFlowView=function(){
  document.getElementById('flow-view').style.display='block';
  document.getElementById('graph-view').style.display='none';
  if(window.chart){try{window.chart.options.plugins.zoom.pan.enabled=false;window.chart.update('none');}catch(e){}}
  _start();
};
window.showGraphView=function(){
  document.getElementById('flow-view').style.display='none';
  document.getElementById('graph-view').style.display='block';
  _stop();
  if(window.chart){try{window.chart.options.plugins.zoom.pan.enabled=true;window.chart.update('none');}catch(e){}}
  if(!window.chart)loadChart(24,'1D',document.querySelector('.btn-period.active'));
};
window.initFlow=function(){
  cv=document.getElementById('flowCv');
  ctx=cv.getContext('2d');
  _fetch();setInterval(_fetch,5000);
  setTimeout(_start,300);
  window.addEventListener('resize',function(){_stop();setTimeout(_start,200);});
};

function _stop(){if(raf){cancelAnimationFrame(raf);raf=null;}}

function _start(){
  if(!cv)return;
  var dpr=window.devicePixelRatio||1;
  W=Math.min(480,window.innerWidth-28);
  H=W*1.12;
  cv.width=W*dpr;cv.height=H*dpr;
  cv.style.width=W+'px';cv.style.height=H+'px';
  ctx.setTransform(1,0,0,1,0,0);
  ctx.scale(dpr,dpr);
  _buildPts();_stop();_draw();
}

function _buildPts(){
  pts=[];
  var pv=parseFloat(fd.pv)||0,ac=parseFloat(fd.ac_out)||0,dc=parseFloat(fd.dc_out)||0;
  if(pv>0)pts=pts.concat(_mk('pv_bat','#22c55e',pv));
  if(fd.ac_on==='ON')pts=pts.concat(_mk('bat_ac','#f97316',ac));
  if(fd.dc_on==='ON')pts=pts.concat(_mk('bat_dc','#f97316',dc>0?dc:5));
}

function _mk(key,col,w){
  var n=Math.min(5,Math.max(1,Math.floor(w/100)));
  var sp=w>300?0.010:w>100?0.006:0.003;
  var ln=Math.min(0.15,Math.max(0.05,w/2500));
  var a=[];for(var k=0;k<n;k++)a.push({key:key,col:col,t:(k/n+k*0.06)%1,sp:sp,ln:ln});
  return a;
}

function _pt(path,t){
  var L=[];
  for(var k=0;k<path.length-1;k++){var dx=path[k+1].x-path[k].x,dy=path[k+1].y-path[k].y;L.push(Math.sqrt(dx*dx+dy*dy));}
  var tot=0;for(var k=0;k<L.length;k++)tot+=L[k];
  var d=t*tot;
  for(var k=0;k<L.length;k++){if(d<=L[k]){var r=d/L[k];return{x:path[k].x+(path[k+1].x-path[k].x)*r,y:path[k].y+(path[k+1].y-path[k].y)*r};}d-=L[k];}
  return path[path.length-1];
}

function _ico(type,cx,cy,r,col){
  ctx.save();ctx.strokeStyle=col;ctx.fillStyle=col;ctx.lineWidth=Math.max(1,r*0.12);
  var s=r*0.48;
  if(type==='pv'){for(var a=-1;a<=1;a++)for(var b=-1;b<=1;b++)ctx.strokeRect(cx+a*s*0.66-s*0.28,cy+b*s*0.66-s*0.28,s*0.56,s*0.56);}
  else if(type==='grid'){ctx.beginPath();ctx.moveTo(cx+s*0.2,cy-s);ctx.lineTo(cx-s*0.3,cy+s*0.1);ctx.lineTo(cx+s*0.05,cy+s*0.1);ctx.lineTo(cx-s*0.2,cy+s);ctx.lineTo(cx+s*0.3,cy-s*0.1);ctx.lineTo(cx-s*0.05,cy-s*0.1);ctx.closePath();ctx.fill();}
  else if(type==='bat'){var w=r,h=r*0.55;ctx.strokeRect(cx-w/2,cy-h/2,w,h);ctx.fillRect(cx+w/2,cy-h*0.22,r*0.1,h*0.44);ctx.fillRect(cx-w*0.25,cy-h*0.22,w*0.5,h*0.44);}
  else if(type==='dc'){ctx.lineWidth=Math.max(2,r*0.15);ctx.beginPath();ctx.moveTo(cx-s*0.6,cy-s*0.2);ctx.lineTo(cx+s*0.6,cy-s*0.2);ctx.stroke();ctx.setLineDash([r*0.12,r*0.1]);ctx.lineWidth=Math.max(1,r*0.08);ctx.beginPath();ctx.moveTo(cx-s*0.6,cy+s*0.2);ctx.lineTo(cx+s*0.6,cy+s*0.2);ctx.stroke();ctx.setLineDash([]);}
  else if(type==='ac'){ctx.beginPath();ctx.arc(cx,cy,s,0,Math.PI*2);ctx.stroke();ctx.beginPath();ctx.moveTo(cx-s*0.45,cy);ctx.bezierCurveTo(cx-s*0.15,cy-s*0.38,cx+s*0.15,cy+s*0.38,cx+s*0.45,cy);ctx.stroke();}
  ctx.restore();
}

function _draw(){
  if(!W||W<10){raf=requestAnimationFrame(_draw);return;}
  ctx.clearRect(0,0,W,H);
  var pv=parseFloat(fd.pv)||0,ac=parseFloat(fd.ac_out)||0;
  var dc=parseFloat(fd.dc_out)||0,gv=parseFloat(fd.grid_v)||0;
  var soc=parseFloat(fd.soc)||0,acOn=fd.ac_on==='ON';
  var tr=fd.time_rem||'--',td=fd.time_rem_dir||0;
  var pad=W*0.06,nR=W*0.09,bR=W*0.17,mx=W/2,my=H*0.48;
  var N={pv:{x:pad+nR,y:H*0.15},grid:{x:W-pad-nR,y:H*0.15},dc:{x:pad+nR,y:H*0.82},ac:{x:W-pad-nR,y:H*0.82}};
  var jT=my-bR*0.35,jB=my+bR*0.35;
  var PA={
    pv_bat:[{x:N.pv.x,y:N.pv.y+nR},{x:N.pv.x,y:jT},{x:mx-bR*0.93,y:jT}],
    grid_bat:[{x:N.grid.x,y:N.grid.y+nR},{x:N.grid.x,y:jT},{x:mx+bR*0.93,y:jT}],
    bat_dc:[{x:mx-bR*0.93,y:jB},{x:N.dc.x,y:jB},{x:N.dc.x,y:N.dc.y-nR}],
    bat_ac:[{x:mx+bR*0.93,y:jB},{x:N.ac.x,y:jB},{x:N.ac.x,y:N.ac.y-nR}]
  };
  var act={pv_bat:pv>0,bat_ac:acOn,bat_dc:dc>0,grid_bat:false};
  var acl={pv_bat:'#22c55e',bat_ac:'#f97316',bat_dc:'#f97316',grid_bat:'#0ea5e9'};
  ctx.lineCap='round';ctx.lineJoin='round';
  var ks=Object.keys(PA);
  for(var ki=0;ki<ks.length;ki++){
    var k=ks[ki],p=PA[k];
    ctx.strokeStyle=act[k]?acl[k]+'44':'#1e293b';ctx.lineWidth=3;
    ctx.beginPath();ctx.moveTo(p[0].x,p[0].y);
    for(var pi=1;pi<p.length-1;pi++)ctx.arcTo(p[pi].x,p[pi].y,p[pi+1].x,p[pi+1].y,16);
    ctx.lineTo(p[p.length-1].x,p[p.length-1].y);ctx.stroke();
  }
  var jd=[[mx-bR*0.93,jT],[mx+bR*0.93,jT],[mx-bR*0.93,jB],[mx+bR*0.93,jB]];
  for(var ji=0;ji<jd.length;ji++){ctx.beginPath();ctx.arc(jd[ji][0],jd[ji][1],3.5,0,Math.PI*2);ctx.fillStyle='#475569';ctx.fill();}
  for(var pi=0;pi<pts.length;pi++){
    var p=pts[pi],path=PA[p.key];if(!path)continue;
    p.t=(p.t+p.sp)%1;
    var h=_pt(path,p.t),tl=_pt(path,Math.max(0,p.t-p.ln));
    var g=ctx.createLinearGradient(tl.x,tl.y,h.x,h.y);
    g.addColorStop(0,p.col+'00');g.addColorStop(1,p.col);
    ctx.strokeStyle=g;ctx.lineWidth=4;ctx.beginPath();ctx.moveTo(tl.x,tl.y);ctx.lineTo(h.x,h.y);ctx.stroke();
  }
  var nd=[
    {k:'pv', t:'pv',  v:pv+'W',  l:'PV',   c:pv>0?'#22c55e':'#475569',  a:pv>0},
    {k:'grid',t:'grid',v:gv+'V', l:'GRID', c:gv>50?'#0ea5e9':'#475569', a:gv>50},
    {k:'dc', t:'dc',  v:dc+'W',  l:'DC',   c:fd.dc_on==='ON'?'#f97316':'#475569',  a:fd.dc_on==='ON'},
    {k:'ac', t:'ac',  v:ac+'W',  l:'AC',   c:acOn?'#f97316':'#475569',  a:acOn}
  ];
  for(var ni=0;ni<nd.length;ni++){
    var n=nd[ni],np=N[n.k];
    ctx.beginPath();ctx.arc(np.x,np.y,nR,0,Math.PI*2);
    ctx.fillStyle=n.a?n.c+'33':'#1e293b';ctx.fill();
    ctx.strokeStyle=n.c;ctx.lineWidth=2;ctx.stroke();
    _ico(n.t,np.x,np.y-nR*0.32,nR*0.35,n.c);
    ctx.textAlign='center';
    ctx.fillStyle=n.c;ctx.font='bold '+Math.round(W*0.030)+'px Courier New';
    ctx.fillText(n.v,np.x,np.y+nR*0.22);
    ctx.globalAlpha=0.65;
    ctx.fillStyle=n.c;ctx.font=Math.round(W*0.024)+'px Courier New';
    ctx.fillText(n.l,np.x,np.y+nR*0.55);
    ctx.globalAlpha=1.0;
  }
  var sc=soc>50?'#22c55e':soc>=30?'#f97316':'#ef4444';
  var sA=Math.PI*0.65,eA=Math.PI*2.35;
  ctx.beginPath();ctx.arc(mx,my,bR,sA,eA);ctx.strokeStyle='#1e293b';ctx.lineWidth=8;ctx.lineCap='round';ctx.stroke();
  ctx.beginPath();ctx.arc(mx,my,bR,sA,sA+(eA-sA)*(soc/100));ctx.strokeStyle=sc;ctx.lineWidth=8;ctx.stroke();
  _ico('bat',mx,my-bR*0.42,bR*0.25,sc);
  ctx.fillStyle=sc;ctx.font='bold '+Math.round(W*0.075)+'px Courier New';ctx.textAlign='center';
  ctx.fillText(soc+'%',mx,my+bR*0.1);
  ctx.font=Math.round(W*0.032)+'px Courier New';
  ctx.fillStyle=td>0?'#22c55e':td<0?'#ef4444':'#475569';
  ctx.fillText(tr,mx,my+bR*0.4);
  raf=requestAnimationFrame(_draw);
}

async function _fetch(){
  try{var r=await fetch('/api/status');fd=await r.json();_buildPts();}catch(e){}
}
})();

function toggleLog(){
  document.getElementById('log-modal').classList.add('show');
}
function closeLogModal(){
  document.getElementById('log-modal').classList.remove('show');
}
function showForecast(day) {
  const w = window._weatherData;
  if (!w) return;
  const isToday = day === 'today';
  document.getElementById('fc-title').textContent = isToday ? 'TODAY FORECAST' : 'TOMORROW FORECAST';
  const hourly = isToday ? w.irr_today : w.irr_tomorrow;
  if (!hourly || !hourly.length) {
    document.getElementById('fc-content').innerHTML = '<div style="color:#475569;text-align:center;padding:20px">No hourly data</div>';
    document.getElementById('fc-modal').classList.add('show');
    return;
  }
  const nowH = new Date().getHours();
  const startH = isToday ? Math.max(nowH, 6) : 6;
  const MAX = 800;
  let html = '';
  hourly.forEach(function(d) {
    if (d.h < startH || d.h > 17) return;
    const kwh = (d.irr * 6 * 0.15 / 1000).toFixed(2);
    const pct = Math.round((d.irr / MAX) * 100);
    const col = d.irr >= 250 ? '#22c55e' : d.irr >= 100 ? '#eab308' : '#64748b';
    html += '<div style="display:flex;align-items:center;padding:8px 0;border-bottom:1px solid #0f172a">'
      + '<span style="font-size:13px;font-weight:700;width:50px">' + String(d.h).padStart(2,'0') + ':00</span>'
      + '<span style="font-size:15px;width:24px;text-align:center">' + d.icon + '</span>'
      + '<div style="flex:1;margin:0 8px;height:4px;background:#0f172a;border-radius:2px"><div style="height:100%;width:' + pct + '%;background:' + col + ';border-radius:2px"></div></div>'
      + '<span style="font-size:11px;color:' + col + ';width:60px;text-align:right">' + d.irr + 'W/m²</span>'
      + '<span style="font-size:12px;font-weight:700;color:' + col + ';width:60px;text-align:right">~' + kwh + 'kWh</span>'
      + '</div>';
  });
  if (!html) html = '<div style="color:#475569;text-align:center;padding:20px">No data for this period</div>';
  document.getElementById('fc-content').innerHTML = html;
  document.getElementById('fc-modal').classList.add('show');
}
fetchStatus();
setInterval(fetchStatus, 5000);

// ── MODAL ────────────────────────────────────────────────────────
function showAcModal() {
  const isOn = currentAcOn === 'ON';
  const action = isOn ? 'OFF' : 'ON';
  pendingAction = { type: 'ac', value: action };
  document.getElementById('modal-title').textContent = 'KONFIRMASI AC';
  document.getElementById('modal-info').innerHTML =
    `AC sekarang: <strong>${currentAcOn}</strong><br>Yakin Turn AC <strong>${action}</strong>?`;
  const confirmBtn = document.getElementById('modal-confirm');
  confirmBtn.textContent = `Ya, Turn ${action}`;
  confirmBtn.style.cssText = isOn
    ? 'background:#dc2626;color:#fff;flex:1;padding:10px;border-radius:6px;font-family:monospace;font-weight:bold;cursor:pointer'
    : 'background:#16a34a;color:#fff;flex:1;padding:10px;border-radius:6px;font-family:monospace;font-weight:bold;cursor:pointer';
  document.getElementById('modal').classList.add('show');
}

function showModal(type) {
  pendingAction = { type };
  const isAuto = type === 'pause';
  document.getElementById('modal-title').textContent = isAuto ? 'PAUSE AUTOMATION' : 'RESUME AUTOMATION';
  document.getElementById('modal-info').innerHTML = isAuto
    ? 'Semua rule akan <strong>berhenti</strong>.<br>Yakin pause automation?'
    : 'Automation akan <strong>aktif kembali</strong>.<br>Yakin resume?';
  const confirmBtn = document.getElementById('modal-confirm');
  confirmBtn.textContent = isAuto ? 'Ya, Pause' : 'Ya, Resume';
  confirmBtn.style.cssText = 'background:#0ea5e9;color:#fff;flex:1;padding:10px;border-radius:6px;font-family:monospace;font-weight:bold;cursor:pointer';
  document.getElementById('modal').classList.add('show');
}

function closeModal() {
  document.getElementById('modal').classList.remove('show');
  pendingAction = null;
}

async function doConfirm() {
  if (!pendingAction) return;
  closeModal();
  try {
    await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(pendingAction)
    });
    setTimeout(fetchStatus, 1000);
  } catch(e) {}
}

// ── SYSTEM STATUS ───────────────────────────────────────────────
let _sysTimer = null;
async function showSystemPopup() {
  document.getElementById('sys-modal').classList.add('show');
  await _fetchSys();
  _sysTimer = setInterval(_fetchSys, 3000);
}
async function _fetchSys() {
  try {
    const r = await fetch('/api/system');
    const d = await r.json();
    const cpuCls = d.cpu > 80 ? 'red' : d.cpu > 50 ? 'yellow' : 'green';
    const tmpCls = d.temp > 70 ? 'red' : d.temp > 55 ? 'yellow' : 'green';
    const ramCls = d.ram_pct > 80 ? 'red' : d.ram_pct > 60 ? 'yellow' : 'green';
    document.getElementById('sys-cpu').innerHTML =
      `<span class="${cpuCls}">${d.cpu}%</span>`;
    document.getElementById('sys-temp').innerHTML =
      `<span class="${tmpCls}">${d.temp}°C</span>`;
    document.getElementById('sys-ram').innerHTML =
      `<span class="${ramCls}">${d.ram_used}MB / ${d.ram_total}MB (${d.ram_pct}%)</span>`;
    document.getElementById('sys-disk').textContent =
      `${d.disk_used}GB / ${d.disk_total}GB (${d.disk_pct}%)`;
    document.getElementById('sys-uptime').textContent = d.uptime;
  } catch(e) {
    document.getElementById('sys-content').innerHTML =
      '<div style="color:#ef4444">Gagal memuat data sistem</div>';
  }
}

function closeSysModal() {
  document.getElementById('sys-modal').classList.remove('show');
  if(_sysTimer){clearInterval(_sysTimer);_sysTimer=null;}
}

async function confirmReboot() {
  if (!confirm('Yakin mau reboot Pi?\nSemua service akan restart ~1 menit.')) return;
  try {
    await fetch('/api/reboot', {method:'POST'});
    closeSysModal();
    alert('Pi is rebooting... wait ~1 minute then refresh.');
  } catch(e) {}
}

// ── CHART ────────────────────────────────────────────────────────
let chart = null;
let currentRules = [];
let filterState = { soc:true, pv:true, acout:true };
const fColors = {
  soc:   {border:'#3b82f6',bg:'#1e3a5f'},
  pv:    {border:'#eab308',bg:'#1a1a00'},
  acout: {border:'#ef4444',bg:'#2d0000'},
};

const ruleBandPlugin = {
  id:'ruleBands',
  beforeDraw(chart) {
    if (!currentRules.length) return;
    const {ctx,chartArea:{left,right,top,bottom},scales:{x}} = chart;
    const labels = chart.data.labels;
    currentRules.forEach((rule,i) => {
      const next = currentRules[i+1];
      const x1i  = labels.findIndex(l => l >= rule.time_str);
      const x2i  = next ? labels.findIndex(l => l >= next.time_str) : labels.length-1;
      if (x1i<0) return;
      const px1 = x.getPixelForValue(x1i);
      const px2 = x.getPixelForValue(x2i<0?labels.length-1:x2i);
      const cL  = Math.max(left,px1), cR = Math.min(right,px2);
      if (cR<=cL) return;
      ctx.save();
      ctx.fillStyle = rule.color;
      ctx.fillRect(cL,top,cR-cL,bottom-top);
      ctx.font='bold 10px Courier New';
      ctx.fillStyle=rule.color.replace('0.18','0.9');
      ctx.fillText(rule.rule,cL+3,top+13);
      ctx.restore();
    });
  }
};

function getActiveRule(ts) {
  let a=null;
  for(const r of currentRules){if(r.time_str<=ts)a=r;else break;}
  return a;
}

// Tooltip selalu muncul di area atas grafik (tidak ikut nilai data)
Chart.Tooltip.positioners.fixedTop = function(items, pos) {
  if (!items.length) return false;
  const chart = this.chart;
  return {
    x: pos.x,
    y: chart.chartArea.top + 4,
  };
};

function buildChart(data) {
  const ctx = document.getElementById('mainChart').getContext('2d');
  if (chart) chart.destroy();
  chart = new Chart(ctx,{
    type:'line',
    data:{
      labels:data.labels,
      datasets:[
        {label:'SOC',data:data.soc,borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,0.04)',
         borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'yPct',hidden:!filterState.soc},
        {label:'PV',data:data.pv,borderColor:'#eab308',backgroundColor:'rgba(234,179,8,0.04)',
         borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'yW',hidden:!filterState.pv},
        {label:'AC OUT',data:data.ac_out,borderColor:'#ef4444',backgroundColor:'rgba(239,68,68,0.04)',
         borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'yW',hidden:!filterState.acout},
      ]
    },
    options:{
      responsive:true,animation:false,
      events:['mousemove','mouseout','click','touchstart','touchmove','touchend'],
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          position:'fixedTop',
          backgroundColor:'#1e293b',borderColor:'#334155',borderWidth:1,
          titleColor:'#94a3b8',bodyColor:'#e2e8f0',padding:10,
          bodyFont:{family:'Courier New',size:12},titleFont:{family:'Courier New',size:11},
          callbacks:{
            title:i=>i[0].label,
            label:i=>{
              const v=i.raw;
              if(i.dataset.label==='SOC')    return ` SOC    : ${v??'--'}%`;
              if(i.dataset.label==='PV')     return ` PV     : ${v??'--'}W`;
              if(i.dataset.label==='AC OUT') return ` AC OUT : ${v??'--'}W`;
              return '';
            },
            afterBody:i=>{
              const r=getActiveRule(i[0].label);
              return r?[``,` ■ ${r.label}`]:[];
            }
          }
        },
        zoom:{
          pan:{enabled:true,mode:'x',threshold:15},
          zoom:{wheel:{enabled:true},pinch:{enabled:true},mode:'x'},
        },
      },
      scales:{
        x:{
          ticks:{color:'#94a3b8',font:{family:'Courier New',size:10},maxTicksLimit:8,maxRotation:0},
          grid:{color:'#1e293b'},
        },
        yPct:{type:'linear',position:'left',min:0,max:100,
          ticks:{color:'#3b82f6',font:{family:'Courier New',size:10},callback:v=>`${v}%`},
          grid:{color:'#1e293b'},
        },
        yW:{type:'linear',position:'right',min:0,
          ticks:{color:'#94a3b8',font:{family:'Courier New',size:10},callback:v=>`${v}W`},
          grid:{display:false},
        },
      },
    },
    plugins:[ruleBandPlugin],
  });

  // Touch: tap = tooltip
  let txs=0,tys=0,panning=false;
  const cv = document.getElementById('mainChart');
  cv.addEventListener('touchstart',e=>{txs=e.touches[0].clientX;tys=e.touches[0].clientY;panning=false},{passive:true});
  cv.addEventListener('touchmove', e=>{if(Math.abs(e.touches[0].clientX-txs)>10||Math.abs(e.touches[0].clientY-tys)>10)panning=true},{passive:true});
  cv.addEventListener('touchend',  e=>{
    if(!panning&&chart){
      const t=e.changedTouches[0],rect=cv.getBoundingClientRect();
      const x=t.clientX-rect.left;
      const idx=Math.round(chart.scales.x.getValueForPixel(x));
      if(idx>=0&&idx<chart.data.labels.length){
        chart.tooltip.setActiveElements(
          chart.data.datasets.map((_,i)=>({datasetIndex:i,index:idx})),{x,y:t.clientY-rect.top}
        );
        chart.update();
      }
    }
  },{passive:true});
}

async function loadChart(hours, label, btn) {
  document.querySelectorAll('.btn-period').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  try {
    const r = await fetch(`/api/chart?hours=${hours}&label=${label}`);
    const d = await r.json();
    currentRules = d.rules;
    buildChart(d);
    renderHealth(d.degradation);
    document.getElementById('sum-title').textContent  = `ENERGY SUMMARY — ${label}`;
    document.getElementById('sum-pv').textContent     = `${d.summary.pv} kWh`;
    document.getElementById('sum-load').textContent   = `${d.summary.load} kWh`;
    const diff=d.summary.diff, el=document.getElementById('sum-diff');
    if(diff>=0){el.textContent=`+${diff.toFixed(3)} kWh ↑`;el.className='summary-value surplus';}
    else{el.textContent=`${diff.toFixed(3)} kWh ↓`;el.className='summary-value deficit';}
  } catch(e) {}
}

function renderHealth(deg) {
  const el = document.getElementById('health-content');
  if (!deg) {
    el.innerHTML = '<div class="health-note">No data yet — first measurement will appear after a valid discharge window is detected (AC ON + PV=0 + stable load at night).</div>';
    return;
  }
  const pct  = deg.deg_pct;
  const cls  = pct < 5 ? 'green' : pct < 15 ? 'yellow' : 'red';
  const note = deg.count < 10
    ? `<div class="health-note">⚠ Accuracy still low (${deg.count} measurements). Improves after 30+ measurements.</div>`
    : `<div class="health-note">✓ ${deg.count} measurements collected.</div>`;
  el.innerHTML = `
    <div class="health-row">
      <span class="health-label">Last</span>
      <span class="summary-value">${deg.last_wh.toLocaleString()} Wh</span>
    </div>
    <div class="health-row">
      <span class="health-label">Waktu ukur</span>
      <span class="summary-value dim">${deg.last_date}</span>
    </div>
    <div class="health-row">
      <span class="health-label">Baseline</span>
      <span class="summary-value">${deg.baseline.toLocaleString()} Wh</span>
    </div>
    <div class="health-row">
      <span class="health-label">Degradation</span>
      <span class="summary-value ${cls}">${pct >= 0 ? '+' : ''}${pct}% ↓</span>
    </div>
    <div class="health-row">
      <span class="health-label">Data points</span>
      <span class="summary-value">${deg.count} measurements</span>
    </div>
    ${note}`;
}

function toggleFilter(key) {
  filterState[key]=!filterState[key];
  const box=document.getElementById('box-'+key);
  const idx={soc:0,pv:1,acout:2}[key];
  if(chart){chart.data.datasets[idx].hidden=!filterState[key];chart.update();}
  if(filterState[key]){box.classList.add('checked');box.style.background=fColors[key].bg;}
  else{box.classList.remove('checked');box.style.background='transparent';}
}

function resetZoom(){if(chart)chart.resetZoom();}
</script>
</body>
</html>
"""

# ================================================================
# ACTION HANDLER
# ================================================================
def do_action(body):
    try:
        data = json.loads(body)
        t = data.get("type")
        if t == "ac" and _mqtt_client:
            _mqtt_client.publish(TOPIC_CMD, data.get("value","OFF"))
        elif t == "pause":
            open(PAUSE_FLAG,"w").close()
            os.system("sudo systemctl stop bluetti")
        elif t == "resume":
            try: os.remove(PAUSE_FLAG)
            except: pass
            os.system("sudo systemctl start bluetti")
    except: pass

# ================================================================
# HTTP HANDLER
# ================================================================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/","/index.html"):
            b = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","text/html;charset=utf-8")
            self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b)

        elif self.path == "/api/status":
            b = json.dumps(get_status()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b)

        elif self.path == "/api/system":
            b = json.dumps(get_system_info()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b)

        elif self.path.startswith("/api/chart"):
            from urllib.parse import urlparse,parse_qs
            qs    = parse_qs(urlparse(self.path).query)
            hours = int(qs.get("hours",["24"])[0])
            label = qs.get("label",["1D"])[0]
            b     = json.dumps(get_chart(hours,label)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b)

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/reboot":
            import subprocess
            subprocess.Popen(["sudo","reboot"])
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        if self.path == "/api/action":
            length = int(self.headers.get("Content-Length",0))
            body   = self.rfile.read(length)
            do_action(body)
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()

    def log_message(self,*args): pass

# ================================================================
# MAIN
# ================================================================
if __name__ == "__main__":
    t = threading.Thread(target=mqtt_thread, daemon=True)
    t.start()
    import time; time.sleep(1.5)
    print(f"Bluetti Dashboard :  http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
