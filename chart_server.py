#!/usr/bin/env python3
"""
chart_server.py — Bluetti Dashboard (2 tab: Control + Chart)
Port    : 8080
Service : chart.service
"""

import os, csv, json, re, subprocess, threading, urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import paho.mqtt.client as mqtt

CSV_FILE      = os.path.expanduser("~/bluetti_history.csv")
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
    "A1":"A1 Pagi ON","A2":"A2 SOC Rendah","A3":"A3 Recovery",
    "A4":"A4 Solar Lemah","A5":"A5 Standby","A6":"A6 PLN Mati","A7":"A7 PLN Hidup",
}

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
            TOPIC_AC_ON,TOPIC_CHG,TOPIC_DCHG
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
                    uptime = f"{h}j {m}m"
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
        ts   = f"{h}j {m}m" if h>0 else f"{m}m"
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
        "grid_v": s["grid_v"], "ac_on": s["ac_on"],
        "time_rem": time_rem, "time_rem_dir": time_rem_dir,
        "mqtt_ok": s["mqtt_ok"], "fresh": fresh, "last_upd": last_upd,
        "bt_active": bt_active, "bt_uptime": bt_uptime,
        "auto_active": auto_active, "auto_paused": paused,
        "last_rule": last_rule,
        "log": log_lines,
        "est_a2": calc_est_a2(),
        "weather": get_weather(),
    }


LAT = -7.884277
LON = 110.311251
_weather_cache = {"data": None, "ts": 0}
WEATHER_CACHE_SEC = 3600

def get_weather():
    import time as _time
    now = _time.time()
    if _weather_cache["data"] and (now - _weather_cache["ts"]) < WEATHER_CACHE_SEC:
        return _weather_cache["data"]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={LAT}&longitude={LON}"
               f"&daily=weathercode,shortwave_radiation_sum"
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
        result = {
            "today":    {"icon": wcode_to_icon(codes[0] if codes else None),    "pv_est": rad_to_kwh(rad[0] if rad else None)},
            "tomorrow": {"icon": wcode_to_icon(codes[1] if len(codes)>1 else None), "pv_est": rad_to_kwh(rad[1] if len(rad)>1 else None)},
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
    if not os.path.exists(CSV_FILE): return []
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = []
    try:
        with open(CSV_FILE) as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.strptime(row["timestamp"],"%Y-%m-%d %H:%M:%S")
                    if ts < cutoff: continue
                    rows.append({
                        "ts":     ts.strftime("%H:%M") if hours<=24 else ts.strftime("%d/%m %H:%M"),
                        "soc":    float(row["soc"])    if row.get("soc")    else None,
                        "pv":     float(row["pv"])     if row.get("pv")     else None,
                        "ac_out": float(row["ac_out"]) if row.get("ac_out") else None,
                    })
                except: continue
    except: pass
    return rows

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
    }

# ================================================================
# HTML
# ================================================================
HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bluetti</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hammer.js/2.0.8/hammer.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.0.1/chartjs-plugin-zoom.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Courier New',monospace;min-height:100vh}

/* TAB NAV */
.tab-nav{display:flex;border-bottom:1px solid #1e293b;background:#0f172a;position:sticky;top:0;z-index:10}
.tab-btn{flex:1;padding:12px 0;font-family:'Courier New',monospace;font-size:13px;
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
.status-label{color:#e2e8f0;font-size:11px;letter-spacing:1px}
.status-value{font-weight:bold}
.soc-bar{height:4px;background:#0f172a;border-radius:2px;margin-top:4px;overflow:hidden}
.soc-fill{height:100%;border-radius:2px;transition:width 0.5s}

/* TOMBOL */
.btn-ac{width:100%;padding:14px;border-radius:8px;border:none;
  font-family:'Courier New',monospace;font-size:14px;font-weight:bold;
  cursor:pointer;margin-bottom:8px;letter-spacing:1px;transition:all 0.15s}
.btn-pause{width:100%;padding:11px;border-radius:8px;border:1px solid #334155;
  background:#1e293b;color:#94a3b8;font-family:'Courier New',monospace;
  font-size:12px;cursor:pointer;margin-bottom:6px;transition:all 0.15s}
.btn-pause:hover{border-color:#0ea5e9;color:#e0f2fe}

/* LOG */
.log-box{background:#020617;border-radius:8px;padding:12px;margin-top:10px;
  max-height:200px;overflow-y:auto;font-size:11px;line-height:1.7}
.log-ts{color:#0ea5e9}
.log-action{color:#22c55e}
.log-detail{color:#64748b}

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
  background:#0f172a;color:#94a3b8;font-family:'Courier New',monospace;cursor:pointer}
.modal-confirm{flex:1;padding:10px;border-radius:6px;border:none;
  font-family:'Courier New',monospace;font-weight:bold;cursor:pointer}

/* CHART TAB */
.period-row{display:flex;gap:8px;margin-bottom:12px}
.btn-period{flex:1;padding:8px 0;border:1px solid #334155;background:#1e293b;
  color:#94a3b8;border-radius:6px;font-family:'Courier New',monospace;font-size:13px;cursor:pointer}
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
.reset-btn{display:block;width:100%;margin-top:10px;padding:8px;
  background:#1e293b;border:1px solid #334155;color:#94a3b8;
  border-radius:6px;font-family:'Courier New',monospace;font-size:12px;cursor:pointer}

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
      <span class="status-label">TIME REM</span>
      <span class="status-value" id="s-timerem">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">EST A2</span>
      <span class="status-value" id="s-esta2">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">PV</span>
      <span class="status-value" id="s-pv">--W</span>
    </div>
    <div class="status-row">
      <span class="status-label">LOAD</span>
      <span class="status-value" id="s-load">--W</span>
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
      <span class="status-label">MQTT</span>
      <span class="status-value" id="s-mqtt">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">DATA</span>
      <span class="status-value" id="s-data">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">LAST UPDATE</span>
      <span class="status-value" id="s-lastupd">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">BLUETTI CONN</span>
      <span class="status-value" id="s-bt">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">AUTOMATION</span>
      <span class="status-value" id="s-auto">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">RULE LAST</span>
      <span class="status-value dim" id="s-rule">--</span>
    </div>
  </div>

  <!-- WEATHER -->
  <div class="status-card" id="weather-card" style="display:none">
    <div class="status-row">
      <span class="status-label">TODAY</span>
      <span class="status-value" id="w-today">--</span>
    </div>
    <div class="status-row">
      <span class="status-label">TOMORROW</span>
      <span class="status-value" id="w-tomorrow">--</span>
    </div>
  </div>

  <!-- TOMBOL AC -->
  <button class="btn-ac" id="btn-ac" onclick="showAcModal()">--</button>

  <!-- TOMBOL AUTOMATION -->
  <button class="btn-pause" onclick="showModal('pause')">⏸ Pause automation</button>
  <button class="btn-pause" onclick="showModal('resume')">▶ Resume automation</button>

  <!-- LOG -->
  <div class="log-box" id="log-box">
    <span class="dim">Memuat log...</span>
  </div>

</div>

<!-- ══════════════ TAB CHART ══════════════ -->
<div id="tab-chart" class="tab-content">

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
    <div class="summary-title" id="sum-title">SUMMARY ENERGI — 1D</div>
    <div class="summary-row">
      <span class="summary-label">☀️  PV dihasilkan</span>
      <span class="summary-value" id="sum-pv">—</span>
    </div>
    <div class="summary-row">
      <span class="summary-label">⚡  AC konsumsi</span>
      <span class="summary-value" id="sum-load">—</span>
    </div>
    <hr class="divider">
    <div class="summary-row">
      <span class="summary-label">📊  Selisih</span>
      <span class="summary-value" id="sum-diff">—</span>
    </div>
  </div>

  <button class="reset-btn" onclick="resetZoom()">↺ Reset zoom</button>

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

<script>
// ── TAB ──────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if (name === 'chart' && !chart) loadChart(24,'1D',document.querySelector('.btn-period.active'));
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

  // MQTT, DATA
  document.getElementById('s-mqtt').innerHTML = d.mqtt_ok
    ? '<span class="green">OK</span>' : '<span class="red">FAIL</span>';
  document.getElementById('s-data').innerHTML = d.fresh
    ? '<span class="green">FRESH</span>' : '<span class="red">STALE</span>';
  document.getElementById('s-lastupd').textContent = d.last_upd;
  document.getElementById('s-lastupd').className = 'status-value ' + (d.fresh ? '' : 'red');

  // BLUETTI CONN
  const btEl = document.getElementById('s-bt');
  btEl.textContent = d.bt_active ? `ACTIVE (${d.bt_uptime})` : 'INACTIVE';
  btEl.className = 'status-value ' + (d.bt_active?'green':'red');

  // AUTOMATION
  const autoEl = document.getElementById('s-auto');
  if (d.auto_paused) { autoEl.textContent='PAUSED'; autoEl.className='status-value yellow'; }
  else if (d.auto_active) { autoEl.textContent='ACTIVE'; autoEl.className='status-value green'; }
  else { autoEl.textContent='INACTIVE'; autoEl.className='status-value red'; }

  // RULE LAST
  document.getElementById('s-rule').textContent = d.last_rule || '--';

  // EST A2
  const a2El = document.getElementById('s-esta2');
  if (d.est_a2 === null || d.est_a2 === undefined) {
    a2El.textContent = '--'; a2El.className = 'status-value dim';
  } else if (d.est_a2 === 0) {
    a2El.textContent = 'SOC ≤ 40%'; a2El.className = 'status-value red';
  } else {
    const h = Math.floor(d.est_a2/60), m = d.est_a2%60;
    const ts = h>0 ? `${h}j ${m}m` : `${m}m`;
    a2El.textContent = ts;
    a2El.className = 'status-value ' + (d.est_a2>120?'green':d.est_a2>60?'yellow':'red');
  }

  // WEATHER
  const wCard = document.getElementById('weather-card');
  if (d.weather) {
    wCard.style.display = '';
    const w = d.weather;
    document.getElementById('w-today').textContent =
      `${w.today.icon} ${w.today.pv_est !== null ? 'est. ~'+w.today.pv_est+' kWh' : '--'}`;
    document.getElementById('w-tomorrow').textContent =
      `${w.tomorrow.icon} ${w.tomorrow.pv_est !== null ? 'est. ~'+w.tomorrow.pv_est+' kWh' : '--'}`;
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
fetchStatus();
setInterval(fetchStatus, 10000);

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
          pan:{enabled:false,mode:'x'},
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
    document.getElementById('sum-title').textContent  = `SUMMARY ENERGI — ${label}`;
    document.getElementById('sum-pv').textContent     = `${d.summary.pv} kWh`;
    document.getElementById('sum-load').textContent   = `${d.summary.load} kWh`;
    const diff=d.summary.diff, el=document.getElementById('sum-diff');
    if(diff>=0){el.textContent=`+${diff.toFixed(3)} kWh ↑`;el.className='summary-value surplus';}
    else{el.textContent=`${diff.toFixed(3)} kWh ↓`;el.className='summary-value deficit';}
  } catch(e) {}
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
        elif t == "resume":
            try: os.remove(PAUSE_FLAG)
            except: pass
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
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
