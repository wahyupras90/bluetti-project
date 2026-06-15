#!/usr/bin/env python3
"""
shadow_logger.py — Data collector untuk analisis A3 berbasis weather forecast
Jalan via cron tiap 5 menit.
Output: ~/bluetti_shadow.csv
Kolom : timestamp, soc, pv_actual, total_out, ac_on, a3_triggered,
        forecast_irr, forecast_irr_next, temp, absorbed_pct, forecast_cached
"""
import os, json, csv, time
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

LAT          = -7.884277
LON          = 110.311251
API_STATUS   = "http://127.0.0.1:8080/api/status"
SHADOW_CSV   = os.path.expanduser("~/bluetti_shadow.csv")
FORECAST_CACHE = "/tmp/shadow_forecast_cache.json"
CACHE_TTL_MIN  = 60

def get_status():
    try:
        r = urlopen(API_STATUS, timeout=5)
        return json.loads(r.read())
    except:
        return None

def get_forecast():
    if os.path.exists(FORECAST_CACHE):
        try:
            with open(FORECAST_CACHE) as f:
                cache = json.load(f)
            if (time.time() - cache["cached_at"]) / 60 < CACHE_TTL_MIN:
                return cache["data"], True
        except:
            pass
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=shortwave_radiation,weathercode,temperature_2m"
        f"&forecast_days=2"
        f"&timezone=Asia%2FJakarta"
    )
    try:
        req = Request(url, headers={"User-Agent": "bluetti-shadow/1.0"})
        r = urlopen(req, timeout=10)
        data = json.loads(r.read())
        with open(FORECAST_CACHE, "w") as f:
            json.dump({"cached_at": time.time(), "data": data}, f)
        return data, False
    except URLError:
        return None, False

def get_current_forecast(data):
    if not data:
        return None, None, None
    try:
        now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
        nxt_str = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        times = data["hourly"]["time"]
        irr   = data["hourly"]["shortwave_radiation"]
        temp  = data["hourly"].get("temperature_2m", [])
        irr_now = irr[times.index(now_str)] if now_str in times else None
        irr_next = irr[times.index(nxt_str)] if nxt_str in times else None
        temp_now = temp[times.index(now_str)] if now_str in times and temp else None
        return (round(irr_now,1) if irr_now else None,
                round(irr_next,1) if irr_next else None,
                round(temp_now,1) if temp_now else None)
    except:
        return None, None, None

def calc_absorbed_pct(pv_est_total):
    """Hitung % PV actual hari ini dari CSV vs estimasi total."""
    if not pv_est_total or pv_est_total <= 0:
        return None
    try:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        pv_today = 0.0
        for path in [os.path.expanduser("~/bluetti_history.csv"), "/tmp/bluetti_history.csv"]:
            if not os.path.exists(path): continue
            with open(path) as f:
                for row in csv.DictReader(f):
                    if row.get("timestamp","").startswith(today_prefix):
                        pv_today += float(row.get("pv") or 0) / 60 / 1000
        return min(round(pv_today / pv_est_total * 100), 100)
    except:
        return None

def is_a3_triggered(soc, pv, ac_on, now):
    h = now.hour + now.minute / 60
    if not (6.5 <= h < 15.5): return 0
    if ac_on == "ON": return 0
    if soc is None or pv is None: return 0
    if h < 11.5:
        return 1 if soc > 65 else 0
    else:
        if (soc > 65 and pv > 200) or soc > 75:
            return 1
    return 0

def append_csv(row):
    file_exists = os.path.exists(SHADOW_CSV)
    with open(SHADOW_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

def main():
    now    = datetime.now()
    status = get_status()
    if not status or not status.get("fresh", False):
        return

    soc       = status.get("soc")
    pv        = status.get("pv")
    total_out = status.get("total_out", 0)
    ac_on     = status.get("ac_on", "OFF")

    forecast_data, cached = get_forecast()
    irr, irr_next, temp = get_current_forecast(forecast_data)

    # Est total hari ini dari weather
    w = status.get("weather", {})
    pv_est_total = w.get("today", {}).get("pv_est") if w else None
    absorbed_pct = calc_absorbed_pct(pv_est_total)

    a3 = is_a3_triggered(soc, pv, ac_on, now)

    row = {
        "timestamp":         now.strftime("%Y-%m-%d %H:%M:%S"),
        "soc":               soc,
        "pv_actual":         pv,
        "total_out":         total_out,
        "ac_on":             ac_on,
        "a3_triggered":      a3,
        "forecast_irr":      irr,
        "forecast_irr_next": irr_next,
        "temp":              temp,
        "absorbed_pct":      absorbed_pct,
        "forecast_cached":   int(cached),
    }
    append_csv(row)

if __name__ == "__main__":
    main()
