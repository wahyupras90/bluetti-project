#!/usr/bin/env python3
"""
watchdog.py — Bluetti Service Watchdog
Jalankan via cron tiap 5 menit:
  */5 * * * * /home/wahyu/bluetti-desalvo/bin/python /home/wahyu/watchdog.py
"""

import os, subprocess, time, csv
from datetime import datetime

CSV_FILE    = os.path.expanduser("~/bluetti_history.csv")
LOG_FILE    = os.path.expanduser("~/bluetti_log.txt")
STALE_MIN   = 5   # menit sebelum dianggap stale

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] WATCHDOG {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except: pass

def last_csv_ts():
    """Ambil timestamp baris terakhir CSV."""
    if not os.path.exists(CSV_FILE):
        return None
    try:
        with open(CSV_FILE, "rb") as f:
            # Baca dari belakang untuk efisiensi
            f.seek(0, 2)
            pos = f.tell()
            buf = b""
            while pos > 0:
                pos = max(0, pos - 512)
                f.seek(pos)
                buf = f.read() if pos == 0 else f.read(512) + buf
                lines = buf.split(b"\n")
                for line in reversed(lines):
                    line = line.decode("utf-8", errors="ignore").strip()
                    if line and not line.startswith("timestamp"):
                        parts = line.split(",")
                        if parts and parts[0]:
                            from datetime import datetime
                            return datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                if pos == 0:
                    break
    except: pass
    return None

def service_active(name):
    try:
        r = subprocess.run(["systemctl","is-active",name],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == "active"
    except: return False

def restart_service(name):
    try:
        subprocess.run(["sudo","systemctl","restart",name],
                       timeout=15, check=True)
        return True
    except: return False

def main():
    now = datetime.now()

    # Cek 1: apakah bluetti.service aktif?
    if not service_active("bluetti"):
        log("bluetti.service INACTIVE → start")
        if restart_service("bluetti"):
            log("bluetti.service berhasil di-start")
        else:
            log("ERROR: gagal start bluetti.service")
        return

    # Cek 2: apakah data MQTT masih segar (dari CSV)?
    last_ts = last_csv_ts()
    if last_ts is None:
        log("CSV kosong — skip cek stale")
        return

    elapsed_min = (now - last_ts).total_seconds() / 60
    if elapsed_min > STALE_MIN:
        log(f"DATA STALE {elapsed_min:.0f}m → restart bluetti")
        if restart_service("bluetti"):
            log("bluetti.service berhasil di-restart")
        else:
            log("ERROR: gagal restart bluetti.service")
    # Kalau fresh, tidak perlu log (agar log tidak penuh)

if __name__ == "__main__":
    main()
