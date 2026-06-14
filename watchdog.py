#!/usr/bin/env python3
import os, subprocess, csv
from datetime import datetime, timedelta

CSV_FILE    = "/tmp/bluetti_history.csv"
LOG_FILE    = os.path.expanduser("~/bluetti_log.txt")
RETRY_FILE  = "/tmp/bluetti_retries"
STALE_MIN   = 10
MAX_RETRIES = 3

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] WATCHDOG {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except: pass

def get_retries():
    try:
        return int(open(RETRY_FILE).read().strip())
    except: return 0

def set_retries(n):
    try:
        open(RETRY_FILE, "w").write(str(n))
    except: pass

def reset_retries():
    try:
        os.remove(RETRY_FILE)
    except: pass

def last_csv_ts():
    if not os.path.exists(CSV_FILE):
        return None
    try:
        last_row = None
        with open(CSV_FILE) as f:
            for row in csv.DictReader(f):
                last_row = row
        if last_row and last_row.get("timestamp"):
            return datetime.strptime(last_row["timestamp"], "%Y-%m-%d %H:%M:%S")
    except: pass
    return None

def service_active(name):
    try:
        r = subprocess.run(["systemctl","is-active",name],
                           capture_output=True,text=True,timeout=3)
        return r.stdout.strip() == "active"
    except: return False

def restart_service(name):
    try:
        subprocess.run(["sudo","systemctl","restart",name],
                       timeout=15,check=True)
        return True
    except: return False

def main():
    if os.path.exists("/tmp/automation_paused"):
        log("PAUSED — skip bluetti check")
        return

    if not service_active("bluetti"):
        log("bluetti.service INACTIVE → start")
        if restart_service("bluetti"):
            log("berhasil di-start")
            reset_retries()
        return

    last_ts = last_csv_ts()
    if last_ts is None:
        log("CSV kosong — skip")
        return

    elapsed_min = (datetime.now() - last_ts).total_seconds() / 60

    if elapsed_min <= STALE_MIN:
        # Data fresh — reset counter
        reset_retries()
        return

    retries = get_retries()

    if retries >= MAX_RETRIES:
        log(f"DATA STALE {elapsed_min:.0f}m — sudah {retries}x restart, perangkat tidak terjangkau, skip")
        return

    log(f"DATA STALE {elapsed_min:.0f}m → restart bluetti (percobaan {retries+1}/{MAX_RETRIES})")
    if restart_service("bluetti"):
        log("berhasil di-restart")
        set_retries(retries + 1)

if __name__ == "__main__":
    main()
