#!/usr/bin/env python3
"""
shadow_logger.py — Data collector untuk analisis A3 berbasis weather forecast
Jalan via cron tiap 5 menit. Tidak mempengaruhi automation sama sekali.

Output: ~/bluetti_shadow.csv
Kolom : timestamp, soc, pv_actual, ac_out, ac_on, a3_triggered,
        forecast_irr, cloudcover, forecast_cached
"""

import os, json, csv, time
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── Konfigurasi ──────────────────────────────────────────────────
LAT          = -7.884277
LON          = 110.311251
API_STATUS   = "http://127.0.0.1:8080/api/status"
SHADOW_CSV   = os.path.expanduser("~/bluetti_shadow.csv")
FORECAST_CACHE = "/tmp/shadow_forecast_cache.json"
CACHE_TTL_MIN  = 60  # refresh forecast tiap 1 jam

# ── Fetch /api/status ─────────────────────────────────────────────
def get_status():
    try:
        r = urlopen(API_STATUS, timeout=5)
        return json.loads(r.read())
    except:
        return None

# ── Fetch Open-Meteo forecast (dengan cache) ──────────────────────
def get_forecast():
    # Cek cache
    if os.path.exists(FORECAST_CACHE):
        try:
            with open(FORECAST_CACHE) as f:
                cache = json.load(f)
            age_min = (time.time() - cache["cached_at"]) / 60
            if age_min < CACHE_TTL_MIN:
                return cache["data"], True  # (data, from_cache)
        except:
            pass

    # Fetch baru
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&hourly=shortwave_radiation,cloud_cover"
        f"&forecast_days=2"
        f"&timezone=Asia%2FJakarta"
    )
    try:
        req = Request(url, headers={"User-Agent": "bluetti-shadow/1.0"})
        r = urlopen(req, timeout=10)
        data = json.loads(r.read())
        # Simpan cache
        with open(FORECAST_CACHE, "w") as f:
            json.dump({"cached_at": time.time(), "data": data}, f)
        return data, False
    except URLError:
        return None, False

# ── Ambil nilai forecast untuk jam sekarang ───────────────────────
def get_current_forecast(data):
    if not data:
        return None, None, None
    try:
        now     = datetime.now()
        now_str = now.strftime("%Y-%m-%dT%H:00")
        nxt_str = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00")
        times   = data["hourly"]["time"]
        irr     = data["hourly"]["shortwave_radiation"]
        cloud   = data["hourly"]["cloud_cover"]
        irr_now  = None
        irr_next = None
        cld      = None
        if now_str in times:
            idx      = times.index(now_str)
            irr_now  = round(irr[idx], 1)
            cld      = cloud[idx]
        if nxt_str in times:
            idx2     = times.index(nxt_str)
            irr_next = round(irr[idx2], 1)
        return irr_now, cld, irr_next
    except:
        pass
    return None, None, None

# ── Cek apakah A3 trigger saat ini ───────────────────────────────
def is_a3_triggered(soc, pv, ac_on, now):
    """
    Replika logika A3 dari automation.py (read-only, tidak eksekusi)
    A3: 06:30-15:30, kondisi SOC/PV terpenuhi, AC sedang OFF
    """
    h = now.hour + now.minute / 60
    if not (6.5 <= h < 15.5):
        return 0
    if ac_on == "ON":
        return 0  # AC sudah ON, A3 tidak relevan
    if soc is None or pv is None:
        return 0

    if h < 11.5:
        # Pagi: SOC > 65 saja
        return 1 if soc > 65 else 0
    else:
        # Siang: tiered
        if (soc > 65 and pv > 200) or soc > 75:
            return 1
    return 0

# ── Tulis ke CSV ──────────────────────────────────────────────────
def append_csv(row):
    file_exists = os.path.exists(SHADOW_CSV)
    with open(SHADOW_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

# ── Main ──────────────────────────────────────────────────────────
def main():
    now    = datetime.now()
    status = get_status()
    if not status:
        return  # chart_server tidak jalan, skip

    soc    = status.get("soc")
    pv     = status.get("pv")
    ac_out = status.get("ac_out")
    ac_on  = status.get("ac_on", "OFF")
    fresh  = status.get("fresh", False)

    if not fresh:
        return  # Data stale, skip

    forecast_data, cached = get_forecast()
    irr, cloud, irr_next = get_current_forecast(forecast_data)

    a3 = is_a3_triggered(soc, pv, ac_on, now)

    row = {
        "timestamp":       now.strftime("%Y-%m-%d %H:%M:%S"),
        "soc":             soc,
        "pv_actual":       pv,
        "ac_out":          ac_out,
        "ac_on":           ac_on,
        "a3_triggered":    a3,
        "forecast_irr":    irr,
        "cloudcover":      cloud,
        "forecast_irr_next": irr_next,
        "forecast_cached":    int(cached),
    }

    append_csv(row)

if __name__ == "__main__":
    main()
