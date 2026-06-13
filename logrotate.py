#!/usr/bin/env python3
"""
logrotate.py — Bluetti Log Rotation
Trim bluetti_history.csv jika > 50MB (hapus baris terlama)
Trim bluetti_log.txt jika > 5MB (hapus baris terlama)
Jalankan via cron tiap tengah malam:
  0 0 * * * /home/wahyu/bluetti-desalvo/bin/python /home/wahyu/logrotate.py
"""

import os
from datetime import datetime

CSV_FILE  = os.path.expanduser("~/bluetti_history.csv")
LOG_FILE  = os.path.expanduser("~/bluetti_log.txt")

CSV_MAX_MB = 50
LOG_MAX_MB = 5

def file_mb(path):
    try:
        return os.path.getsize(path) / 1024 / 1024
    except: return 0

def trim_csv(path, max_mb):
    if not os.path.exists(path): return
    size = file_mb(path)
    if size <= max_mb: return

    print(f"[logrotate] CSV {size:.1f}MB > {max_mb}MB, trimming...")
    try:
        with open(path, "r") as f:
            lines = f.readlines()

        header = lines[0] if lines else ""
        data   = lines[1:] if len(lines) > 1 else []

        # Hapus 20% baris terlama
        trim_count = max(1, len(data) // 5)
        data = data[trim_count:]

        with open(path, "w") as f:
            f.write(header)
            f.writelines(data)

        new_size = file_mb(path)
        print(f"[logrotate] CSV trimmed: {size:.1f}MB → {new_size:.1f}MB ({trim_count} baris dihapus)")
    except Exception as e:
        print(f"[logrotate] ERROR trim CSV: {e}")

def trim_log(path, max_mb):
    if not os.path.exists(path): return
    size = file_mb(path)
    if size <= max_mb: return

    print(f"[logrotate] LOG {size:.1f}MB > {max_mb}MB, trimming...")
    try:
        with open(path, "r") as f:
            lines = f.readlines()

        # Simpan 70% baris terbaru
        keep = max(100, int(len(lines) * 0.7))
        lines = lines[-keep:]

        with open(path, "w") as f:
            f.writelines(lines)

        new_size = file_mb(path)
        print(f"[logrotate] LOG trimmed: {size:.1f}MB → {new_size:.1f}MB")
    except Exception as e:
        print(f"[logrotate] ERROR trim LOG: {e}")

if __name__ == "__main__":
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[logrotate] {ts}")
    trim_csv(CSV_FILE, CSV_MAX_MB)
    trim_log(LOG_FILE, LOG_MAX_MB)
    print(f"[logrotate] done")
