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
DEG_FILE   = os.path.expanduser("~/bluetti_degradation.csv")
WINDOW_MIN = 30
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
    """Baca 30 menit terakhir dari history CSV."""
    if not os.path.exists(CSV_FILE):
        return []
    cutoff = datetime.now() - timedelta(minutes=WINDOW_MIN)
    rows = []
    try:
        with open(CSV_FILE) as f:
            for row in csv.DictReader(f):
                try:
                    ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    if ts < cutoff:
                        continue
                    soc    = float(row["soc"])    if row.get("soc")    else None
                    pv     = float(row["pv"])     if row.get("pv")     else None
                    ac_out = float(row.get("total_out") or row.get("ac_out") or 0)
                    ac_on  = row.get("ac_on","").upper()
                    rows.append({
                        "ts": ts, "soc": soc, "pv": pv,
                        "ac_out": ac_out, "ac_on": ac_on,
                    })
                except: continue
    except: pass
    return rows

def validate_window(rows):
    """
    Validasi window discharge bersih.
    Return (valid, reason, stats) 
    """
    if len(rows) < MIN_POINTS:
        return False, f"data kurang ({len(rows)}/{MIN_POINTS})", {}

    # Filter baris valid (semua field tersedia)
    valid = [r for r in rows if all(
        r[k] is not None for k in ["soc","pv","ac_out","ac_on"]
    )]

    if len(valid) < MIN_POINTS:
        return False, f"data valid kurang ({len(valid)}/{MIN_POINTS})", {}

    # Syarat 1: ada beban (total_out > 10W)
    if not all(r["ac_out"] > 10 for r in valid):
        return False, "tidak ada beban cukup (total_out <= 10W)", {}

    # Syarat 2: PV = 0 semua (tidak ada solar)
    if any(r["pv"] > 5 for r in valid):  # toleransi 5W noise
        return False, "ada solar input (PV > 5W)", {}

    # Syarat 3: SOC turun
    soc_start = valid[0]["soc"]
    soc_end   = valid[-1]["soc"]
    if soc_end >= soc_start:
        return False, "SOC tidak turun (charging atau flat)", {}

    delta_soc = soc_start - soc_end
    if delta_soc < 1.0:
        return False, f"delta SOC terlalu kecil ({delta_soc:.1f}%)", {}

    # Syarat 4: Beban konsisten < 10% variasi
    loads = [r["ac_out"] for r in valid if r["ac_out"] > 0]
    if not loads:
        return False, "tidak ada data beban", {}

    avg_load = sum(loads) / len(loads)
    if avg_load < 10:
        return False, f"beban terlalu kecil ({avg_load:.0f}W)", {}

    max_dev = max(abs(l - avg_load) / avg_load for l in loads)
    if max_dev > LOAD_VAR:
        return False, f"variasi beban terlalu besar ({max_dev*100:.0f}%)", {}

    # Hitung kapasitas efektif
    duration_h = (valid[-1]["ts"] - valid[0]["ts"]).total_seconds() / 3600
    if duration_h < 0.1:
        return False, "durasi terlalu pendek", {}

    eff_capacity = avg_load * duration_h / (delta_soc / 100)

    # Sanity check: kapasitas harus masuk akal (500-3000 Wh)
    if not (500 <= eff_capacity <= 3000):
        return False, f"kapasitas tidak masuk akal ({eff_capacity:.0f}Wh)", {}

    return True, "OK", {
        "eff_capacity": round(eff_capacity),
        "avg_load":     round(avg_load),
        "soc_start":    round(soc_start, 1),
        "soc_end":      round(soc_end, 1),
        "duration_min": round(duration_h * 60),
        "valid_points": len(valid),
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
