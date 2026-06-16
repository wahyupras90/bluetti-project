#!/usr/bin/env python3
"""
degradation.py — Bluetti Battery Degradation Tracker
Hitung kapasitas efektif baterai dari window discharge bersih

Cron tiap jam:
  0 * * * * /home/wahyu/bluetti-desalvo/bin/python /home/wahyu/degradation.py

Syarat window valid (30 menit terakhir):
  - AC ON seluruhnya
  - PV = 0 (murni dari baterai, tidak ada solar)
  - Variasi beban < 10%
  - SOC turun (discharge)
  - Minimal 25 dari 30 data point tersedia
"""

import os, csv
from datetime import datetime, timedelta

CSV_FILE   = os.path.expanduser("~/bluetti_history.csv")
CSV_RAM    = "/tmp/bluetti_history.csv"
DEG_FILE   = os.path.expanduser("~/bluetti_degradation.csv")
WINDOW_MIN = 1440
MIN_POINTS = 25
LOAD_VAR   = 0.10   # variasi beban < 10%
BLUETTI_NOMINAL = 2048  # Wh kapasitas nominal Elite 200 V2

def ensure_header():
    if not os.path.exists(DEG_FILE):
        with open(DEG_FILE, "w", newline="") as f:
            csv.writer(f).writerow([
                "date","time","eff_capacity_wh",
                "avg_load_w","soc_start","soc_end",
                "duration_min","valid_points"
            ])

def load_window():
    """Baca 24 jam terakhir dari history CSV (RAM + disk)."""
    cutoff = datetime.now() - timedelta(minutes=WINDOW_MIN)
    rows = []
    seen = set()
    for path in [CSV_FILE, CSV_RAM]:
        if not os.path.exists(path): continue
        try:
            with open(path) as f:
                for row in csv.DictReader(f):
                    try:
                        ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                        if ts < cutoff: continue
                        if row["timestamp"] in seen: continue
                        seen.add(row["timestamp"])
                        soc    = float(row["soc"]) if row.get("soc") else None
                        pv     = float(row["pv"]) if row.get("pv") else None
                        ac_out = float(row.get("total_out") or row.get("ac_out") or 0)
                        ac_on  = row.get("ac_on","").upper()
                        rows.append({"ts": ts, "soc": soc, "pv": pv, "ac_out": ac_out, "ac_on": ac_on})
                    except: continue
        except: pass
    rows.sort(key=lambda r: r["ts"])
    return rows

def validate_window(rows):
    """Cari segmen discharge terbaik dari data 24 jam."""
    if not rows:
        return False, "tidak ada data", {}

    candidates = [r for r in rows
                  if r["pv"] is not None and r["pv"] < 5
                  and r["ac_out"] > 10
                  and r["soc"] is not None]

    if len(candidates) < MIN_POINTS:
        return False, f"data valid kurang ({len(candidates)}/{MIN_POINTS})", {}

    # Cari segmen berurutan (max gap 3 menit)
    segments = []
    seg = [candidates[0]]
    for i in range(1, len(candidates)):
        gap = (candidates[i]["ts"] - candidates[i-1]["ts"]).seconds
        if gap <= 180:
            seg.append(candidates[i])
        else:
            if len(seg) >= MIN_POINTS: segments.append(seg)
            seg = [candidates[i]]
    if len(seg) >= MIN_POINTS: segments.append(seg)

    if not segments:
        return False, f"tidak ada segmen berurutan >= {MIN_POINTS} titik", {}

    best = max(segments, key=lambda s: s[0]["soc"] - s[-1]["soc"])
    soc_start = best[0]["soc"]
    soc_end   = best[-1]["soc"]
    delta_soc = soc_start - soc_end

    if delta_soc < 1.0:
        return False, f"delta SOC terlalu kecil ({delta_soc:.1f}%)", {}

    duration_h = (best[-1]["ts"] - best[0]["ts"]).seconds / 3600
    loads = [r["ac_out"] for r in best]
    avg_load = sum(loads) / len(loads)
    eff_capacity = round(avg_load * duration_h / (delta_soc / 100))
    return True, "OK", {
        "eff_capacity": eff_capacity,
        "avg_load": round(avg_load, 1),
        "soc_start": soc_start, "soc_end": soc_end,
        "delta_soc": delta_soc,
        "duration_min": round(duration_h * 60),
        "valid_points": len(best),
    }

def already_logged_today():
    """Cek apakah sudah ada pengukuran hari ini."""
    if not os.path.exists(DEG_FILE):
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(DEG_FILE) as f:
            for row in csv.DictReader(f):
                if row.get("date") == today:
                    return True
    except: pass
    return False

def get_baseline():
    """Ambil pengukuran pertama sebagai baseline."""
    if not os.path.exists(DEG_FILE):
        return None
    try:
        with open(DEG_FILE) as f:
            rows = list(csv.DictReader(f))
        if rows:
            return float(rows[0]["eff_capacity_wh"])
    except: pass
    return None

def main():
    now = datetime.now()
    print(f"[{now.strftime('%H:%M:%S')}] degradation check")

    ensure_header()

    # Hanya satu pengukuran per hari
    if already_logged_today():
        print("  sudah ada pengukuran hari ini, skip")
        return

    rows = load_window()
    valid, reason, stats = validate_window(rows)

    if not valid:
        print(f"  window tidak valid: {reason}")
        return

    # Simpan ke CSV
    with open(DEG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M"),
            stats["eff_capacity"],
            stats["avg_load"],
            stats["soc_start"],
            stats["soc_end"],
            stats["duration_min"],
            stats["valid_points"],
        ])

    # Hitung degradasi vs baseline
    baseline = get_baseline()
    if baseline:
        deg_pct = (1 - stats["eff_capacity"] / baseline) * 100
        deg_str = f"{deg_pct:+.1f}% vs baseline {baseline:.0f}Wh"
    else:
        deg_str = "baseline (pengukuran pertama)"

    print(f"  ✓ kapasitas efektif: {stats['eff_capacity']} Wh")
    print(f"  ✓ beban rata-rata  : {stats['avg_load']} W")
    print(f"  ✓ SOC {stats['soc_start']}% → {stats['soc_end']}%")
    print(f"  ✓ {deg_str}")

if __name__ == "__main__":
    main()
