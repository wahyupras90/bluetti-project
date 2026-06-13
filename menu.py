#!/usr/bin/env python3
"""
menu.py — Bluetti Control Menu
SSH terminal interactive menu untuk Pi OS Lite
Jalankan: python3 menu.py

Kebutuhan:
  pip install paho-mqtt
  sudo visudo → tambah NOPASSWD untuk systemctl restart bluetti/automation
"""

import os
import sys
import select
import time
import threading
import subprocess
from datetime import datetime
import paho.mqtt.client as mqtt

# ================================================================
# KONFIGURASI — sesuaikan sebelum pakai
# ================================================================
MQTT_BROKER  = "127.0.0.1"
MQTT_PORT    = 1883
DEVICE_NAME  = "PR200V2-2551110318791"  # confirmed dari mosquitto_sub Pi

TOPIC_SOC    = f"bluetti/state/{DEVICE_NAME}/total_battery_percent"
TOPIC_PV     = f"bluetti/state/{DEVICE_NAME}/dc_input_power"
TOPIC_AC_OUT = f"bluetti/state/{DEVICE_NAME}/ac_output_power"
TOPIC_GRID_V = f"bluetti/state/{DEVICE_NAME}/ac_input_voltage"
TOPIC_AC_ON  = f"bluetti/state/{DEVICE_NAME}/ac_output_on"
TOPIC_CMD    = f"bluetti/command/{DEVICE_NAME}/ac_output_on"
TOPIC_CHG    = f"bluetti/state/{DEVICE_NAME}/total_battery_charge_time"
TOPIC_DCHG   = f"bluetti/state/{DEVICE_NAME}/total_battery_discharge_time"

TIME_BALANCE_W = 10  # Watt threshold: selisih < ini = BALANCE

LOG_FILE      = os.path.expanduser("~/bluetti_log.txt")
LAST_RULE_FILE= os.path.expanduser("~/bluetti_last_rule.txt")
PAUSE_FLAG    = "/tmp/automation_paused"

STALE_SEC     = 90    # detik sebelum DATA dianggap STALE
HEADER_REFRESH= 10    # detik interval refresh header
REALTIME_REFRESH = 3  # detik interval monitor realtime

# ================================================================
# ANSI COLOR
# ================================================================
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    DIM    = "\033[2m"

def green(s):  return f"{C.GREEN}{s}{C.RESET}"
def yellow(s): return f"{C.YELLOW}{s}{C.RESET}"
def red(s):    return f"{C.RED}{s}{C.RESET}"
def cyan(s):   return f"{C.CYAN}{s}{C.RESET}"
def bold(s):   return f"{C.BOLD}{s}{C.RESET}"
def dim(s):    return f"{C.DIM}{s}{C.RESET}"

def color_soc(val):
    try:
        v = float(val)
        if v > 50:  return green(f"{v:.0f}%")
        if v > 30:  return yellow(f"{v:.0f}%")
        return red(f"{v:.0f}%")
    except:
        return dim(str(val))

def color_grid(val):
    try:
        v = float(val)
        if v >= 200: return green(f"{v:.0f}V")
        if v >= 50:  return yellow(f"{v:.0f}V")
        return red(f"{v:.0f}V")
    except:
        return dim(str(val))

def color_ac(val):
    if val == "ON":  return green("ON")
    if val == "OFF": return red("OFF")
    return dim(str(val))

def color_data(stale):
    return red("STALE") if stale else green("FRESH")

def color_service(active):
    return green("ACTIVE") if active else red("INACTIVE")

def color_automation():
    if os.path.exists(PAUSE_FLAG):
        return yellow("PAUSED")
    ok = service_active("automation")
    if ok:  return green("ACTIVE")
    return red("INACTIVE")

def color_time_rem():
    """Hitung dan format TIME REM berdasarkan PV vs LOAD."""
    with state_lock:
        pv       = state["pv"]
        ac_out   = state["ac_out"]
        chg_time = state["chg_time"]
    if chg_time is None:
        return dim("--")
    try:
        pv_w   = float(pv)
        load_w = float(ac_out)
    except (ValueError, TypeError):
        return dim("--")
    mins = chg_time
    h = mins // 60
    m = mins % 60
    time_str = f"{h}j {m}m" if h > 0 else f"{m}m"
    diff = pv_w - load_w
    if diff > TIME_BALANCE_W:
        return green(f"{time_str} ↑")
    elif diff < -TIME_BALANCE_W:
        return red(f"{time_str} ↓")
    else:
        return dim("--")


def soc_bar(val):
    try:
        v = int(float(val))
        filled = v // 10
        empty  = 10 - filled
        return "█" * filled + "░" * empty
    except:
        return "░" * 10

# ================================================================
# STATE GLOBAL
# ================================================================
state = {
    "soc": "--", "pv": "--", "ac_out": "--",
    "grid_v": "--", "ac_on": "--",
    "chg_time": None,
    "last_ts": None,
    "mqtt_ok": False,
}
state_lock = threading.Lock()

# Toggle untuk indikator realtime
_toggle = ["○"]

def toggle_indicator():
    _toggle[0] = "●" if _toggle[0] == "○" else "○"
    return _toggle[0]

# ================================================================
# MQTT
# ================================================================
_mqtt_client = None

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        with state_lock:
            state["mqtt_ok"] = True
        client.subscribe([
            (TOPIC_SOC, 0), (TOPIC_PV, 0), (TOPIC_AC_OUT, 0),
            (TOPIC_GRID_V, 0), (TOPIC_AC_ON, 0),
            (TOPIC_CHG, 0), (TOPIC_DCHG, 0),
        ])

def on_disconnect(client, userdata, rc):
    with state_lock:
        state["mqtt_ok"] = False

def on_message(client, userdata, msg):
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    with state_lock:
        try:
            val = float(payload)
            if topic == TOPIC_SOC:    state["soc"]    = f"{val:.0f}"
            elif topic == TOPIC_PV:   state["pv"]     = f"{val:.0f}"
            elif topic == TOPIC_AC_OUT: state["ac_out"] = f"{val:.0f}"
            elif topic == TOPIC_GRID_V: state["grid_v"] = f"{val:.1f}"
            elif topic in (TOPIC_CHG, TOPIC_DCHG):
                state["chg_time"] = int(val)
        except ValueError:
            if topic == TOPIC_AC_ON:
                state["ac_on"] = payload.upper()
        state["last_ts"] = time.time()

def mqtt_thread_fn():
    global _mqtt_client
    _mqtt_client = mqtt.Client()
    _mqtt_client.on_connect    = on_connect
    _mqtt_client.on_message    = on_message
    _mqtt_client.on_disconnect = on_disconnect
    while True:
        try:
            _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            _mqtt_client.loop_forever()
        except Exception:
            with state_lock:
                state["mqtt_ok"] = False
            time.sleep(5)

def publish(topic, payload):
    if _mqtt_client:
        _mqtt_client.publish(topic, payload)

# ================================================================
# HELPER: SERVICE STATUS
# ================================================================
def service_active(name):
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip() == "active"
    except:
        return False

def service_uptime(name):
    """Return uptime string misal '18h 23m' atau 'unknown'"""
    try:
        r = subprocess.run(
            ["systemctl", "show", name, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=3
        )
        line = r.stdout.strip()
        if "=" not in line:
            return "unknown"
        ts_str = line.split("=", 1)[1].strip()
        if not ts_str:
            return "unknown"
        # Parse: "Sat 2026-06-07 06:19:44 WIB"
        from datetime import datetime
        import locale
        # Ambil bagian tanggal+waktu saja (skip nama hari)
        parts = ts_str.split()
        if len(parts) >= 3:
            dt_str = f"{parts[1]} {parts[2]}"
            try:
                started = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                delta   = datetime.now() - started
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                return f"{h}h {m}m"
            except:
                pass
        return "unknown"
    except:
        return "unknown"

def last_rule():
    try:
        if os.path.exists(LAST_RULE_FILE):
            with open(LAST_RULE_FILE) as f:
                return f.read().strip() or "--"
    except:
        pass
    return "--"

def last_update_str():
    with state_lock:
        ts = state["last_ts"]
    if ts is None:
        return "never"
    elapsed = time.time() - ts
    if elapsed < 60:
        return f"{int(elapsed)}s ago"
    return f"{int(elapsed//60)}m ago"

def is_stale():
    with state_lock:
        ts = state["last_ts"]
    if ts is None:
        return True
    return (time.time() - ts) > STALE_SEC

# ================================================================
# RENDER HEADER
# ================================================================
def render_header(indicator=None):
    with state_lock:
        soc    = state["soc"]
        pv     = state["pv"]
        ac_out = state["ac_out"]
        grid_v = state["grid_v"]
        ac_on  = state["ac_on"]
        mqtt_ok= state["mqtt_ok"]

    now      = datetime.now().strftime("%H:%M:%S")
    uptime   = service_uptime("bluetti")
    desalvo_status = color_service(service_active("bluetti"))
    auto_status    = color_automation()
    data_status    = color_data(is_stale())
    mqtt_status    = green("OK") if mqtt_ok else red("FAIL")
    last_upd       = last_update_str()
    rule_last      = last_rule()

    ind = f"  {indicator}" if indicator else ""

    lines = []
    lines.append(bold("================================="))
    lines.append(bold(f" BLUETTI CONTROL MENU{ind}"))
    lines.append(bold("================================="))
    lines.append("")
    lines.append(f"TIME         : {cyan(now)}")
    lines.append(f"SOC          : {color_soc(soc)}   {soc_bar(soc)}")
    lines.append(f"TIME REM     : {color_time_rem()}")
    lines.append(f"PV           : {pv}W")
    lines.append(f"LOAD         : {ac_out}W")
    lines.append(f"GRID         : {color_grid(grid_v)}")
    lines.append(f"AC           : {color_ac(ac_on)}")
    lines.append(f"MQTT         : {mqtt_status}")
    lines.append(f"DATA         : {data_status}")
    lines.append(f"LAST UPDATE  : {last_upd}")
    lines.append(f"BLE SVC      : {desalvo_status} ({uptime})")
    lines.append(f"AUTOMATION   : {auto_status}")
    lines.append(f"RULE LAST    : {dim(rule_last)}")
    lines.append("")
    lines.append(dim("---------------------------------"))
    return "\n".join(lines)

def render_menu():
    with state_lock:
        ac_on = state["ac_on"]

    if ac_on == "ON":
        ac_label = f"Turn {red('AC OFF')}  [sekarang: {green('ON')}]"
    elif ac_on == "OFF":
        ac_label = f"Turn {green('AC ON')}   [sekarang: {red('OFF')}]"
    else:
        ac_label = f"Toggle AC  [sekarang: {dim(ac_on)}]"

    lines = []
    lines.append("")
    lines.append(f"  {cyan('1.')} Monitor realtime")
    lines.append(f"  {cyan('2.')} Show automation log")
    lines.append(f"  {cyan('3.')} {ac_label}")
    lines.append(f"  {cyan('4.')} Restart Bluetti service")
    lines.append(f"  {cyan('5.')} Restart automation")
    lines.append(f"  {cyan('6.')} Pause automation")
    lines.append(f"  {cyan('7.')} Resume automation")
    lines.append(f"  {cyan('0.')} Exit")
    lines.append("")
    lines.append(bold("================================="))
    return "\n".join(lines)

# ================================================================
# LAYAR UTAMA (dengan auto-refresh header)
# ================================================================
_refresh_stop = threading.Event()
_in_submenu   = threading.Event()

def clear():
    os.system("clear")

def draw_main():
    clear()
    print(render_header())
    print(render_menu())
    print(f"  Select: ", end="", flush=True)

def auto_refresh_thread():
    """Refresh header tiap HEADER_REFRESH detik di menu utama"""
    while not _refresh_stop.is_set():
        time.sleep(HEADER_REFRESH)
        if not _in_submenu.is_set() and not _refresh_stop.is_set():
            # Simpan posisi cursor tidak bisa di terminal biasa,
            # jadi redraw seluruh layar
            draw_main()

# ================================================================
# KONFIRMASI GENERIC
# ================================================================
def confirm(header_lines, default_n=True):
    """
    Tampilkan kotak konfirmasi.
    Return True kalau user ketik y/Y, False untuk lainnya.
    """
    print()
    print(bold("  ┌─────────────────────────────┐"))
    for line in header_lines:
        padded = line.ljust(29)
        print(f"  │  {padded}│")
    print(bold("  │                             │"))
    print(bold("  │  Yakin? (y/n) :             │"))
    print(bold("  └─────────────────────────────┘"))
    try:
        ans = input("  → ").strip().lower()
        return ans == "y"
    except (KeyboardInterrupt, EOFError):
        return False

# ================================================================
# OPSI 1: MONITOR REALTIME
# ================================================================
def monitor_realtime():
    _in_submenu.set()
    try:
        ind = "○"
        while True:
            ind = "●" if ind == "○" else "○"
            clear()
            with state_lock:
                soc    = state["soc"]
                pv     = state["pv"]
                ac_out = state["ac_out"]
                grid_v = state["grid_v"]
                ac_on  = state["ac_on"]

            now = datetime.now().strftime("%H:%M:%S")
            print(bold("================================="))
            print(bold(f" BLUETTI REALTIME  {ind}  {cyan(now)}"))
            print(bold("================================="))
            print()
            print(f"  SOC    : {color_soc(soc)}   {soc_bar(soc)}")
            print(f"  TIME   : {color_time_rem()}")
            print(f"  PV     : {pv}W")
            print(f"  LOAD   : {ac_out}W")
            print(f"  GRID   : {color_grid(grid_v)}")
            print(f"  AC     : {color_ac(ac_on)}")
            print()
            print(dim("─────────────────────────────────"))
            stale = is_stale()
            data_ind = red("⚠ STALE") if stale else green("● FRESH")
            print(dim(f"  {data_ind} · interval {REALTIME_REFRESH}s"))
            print(dim("  [Enter] kembali ke menu"))

            if select.select([sys.stdin], [], [], REALTIME_REFRESH)[0]:
                input()
                break
    except KeyboardInterrupt:
        pass
    finally:
        _in_submenu.clear()

# ================================================================
# OPSI 2: AUTOMATION LOG
# ================================================================
def show_log():
    _in_submenu.set()
    try:
        clear()
        print(bold("================================="))
        print(bold(" AUTOMATION LOG (20 terakhir)"))
        print(bold("================================="))
        print(dim(" Ctrl+C kembali ke menu"))
        print(dim("---------------------------------"))
        print()
        if not os.path.exists(LOG_FILE):
            print(dim("  (log belum ada — automation belum trigger)"))
        else:
            with open(LOG_FILE) as f:
                lines = f.readlines()
            # Ambil 20 baris terakhir (log per-event bisa multi-baris)
            last = lines[-60:] if len(lines) > 60 else lines
            for line in last:
                line = line.rstrip()
                if line.startswith("["):
                    print(f"  {cyan(line)}")
                elif line.strip().startswith("→"):
                    print(f"  {green(line)}")
                else:
                    print(f"  {line}")
        print()
        print(dim("---------------------------------"))
        input(dim("  (Enter untuk kembali)"))
    except KeyboardInterrupt:
        pass
    finally:
        _in_submenu.clear()

# ================================================================
# OPSI 3: AC TOGGLE
# ================================================================
def toggle_ac():
    _in_submenu.set()
    try:
        with state_lock:
            ac_on = state["ac_on"]

        if ac_on == "ON":
            aksi    = "Turn OFF"
            payload = "OFF"
        elif ac_on == "OFF":
            aksi    = "Turn ON"
            payload = "ON"
        else:
            print(f"\n  {yellow('Status AC belum diketahui, coba lagi.')}")
            time.sleep(2)
            return

        ok = confirm([
            f"AC sekarang  : {ac_on}",
            f"Aksi         : {aksi}",
        ])
        if not ok:
            print(f"\n  {dim('Dibatalkan.')}")
            time.sleep(1)
            return

        publish(TOPIC_CMD, payload)
        print(f"\n  {green('✓')} Command sent: AC {payload}")
        print(f"  {dim('Menunggu konfirmasi...')}")

        # Tunggu konfirmasi max 5 detik
        deadline = time.time() + 5
        confirmed = False
        while time.time() < deadline:
            time.sleep(0.5)
            with state_lock:
                current = state["ac_on"]
            if current == payload:
                confirmed = True
                break

        if confirmed:
            print(f"  {green('✓')} AC : {color_ac(payload)}")
        else:
            print(f"\n  {yellow('⚠')} No confirmation received (timeout 5s)")
            print(f"  {dim('Command may have been sent — check status')}")
        time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        _in_submenu.clear()

# ================================================================
# OPSI 4 & 5: RESTART SERVICE
# ================================================================
def restart_service(name, label, efek):
    _in_submenu.set()
    try:
        uptime = service_uptime(name)
        ok = confirm([
            f"{label:<13}: ACTIVE ({uptime})",
            f"Aksi         : RESTART",
            f"Efek         : {efek}",
        ])
        if not ok:
            print(f"\n  {dim('Dibatalkan.')}")
            time.sleep(1)
            return

        print(f"\n  {dim('Restarting...')}")
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", name],
                timeout=10, check=True
            )
            print(f"  {green('✓')} {label} restarted")
        except subprocess.CalledProcessError:
            print(f"  {red('✗')} Gagal restart {label}")
            print(f"  {dim('Cek: sudo systemctl status ' + name)}")
        except subprocess.TimeoutExpired:
            print(f"  {yellow('⚠')} Timeout saat restart {label}")
        time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        _in_submenu.clear()

# ================================================================
# OPSI 6 & 7: PAUSE / RESUME AUTOMATION
# ================================================================
def pause_automation():
    _in_submenu.set()
    try:
        if os.path.exists(PAUSE_FLAG):
            print(f"\n  {yellow('Automation sudah dalam keadaan PAUSED.')}")
            time.sleep(2)
            return

        ok = confirm([
            "Automation   : ACTIVE",
            "Aksi         : PAUSE",
            "Efek         : semua rule",
            "               berhenti",
        ])
        if not ok:
            print(f"\n  {dim('Dibatalkan.')}")
            time.sleep(1)
            return

        open(PAUSE_FLAG, "w").close()
        print(f"\n  {yellow('⏸')} Automation PAUSED")
        print(f"  {dim('Pilih Resume untuk mengaktifkan kembali.')}")
        time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        _in_submenu.clear()

def resume_automation():
    _in_submenu.set()
    try:
        if not os.path.exists(PAUSE_FLAG):
            print(f"\n  {green('Automation sudah ACTIVE.')}")
            time.sleep(2)
            return

        ok = confirm([
            "Automation   : PAUSED",
            "Aksi         : RESUME",
            "Efek         : semua rule",
            "               aktif kembali",
        ])
        if not ok:
            print(f"\n  {dim('Dibatalkan.')}")
            time.sleep(1)
            return

        try:
            os.remove(PAUSE_FLAG)
            print(f"\n  {green('▶')} Automation RESUMED")
        except FileNotFoundError:
            print(f"\n  {green('▶')} Automation sudah ACTIVE")
        time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        _in_submenu.clear()

# ================================================================
# MAIN LOOP
# ================================================================
def main():
    # Start MQTT thread
    t_mqtt = threading.Thread(target=mqtt_thread_fn, daemon=True)
    t_mqtt.start()

    # Tunggu sebentar biar MQTT sempat konek
    time.sleep(1.5)

    # Start auto-refresh thread
    t_refresh = threading.Thread(target=auto_refresh_thread, daemon=True)
    t_refresh.start()

    try:
        while True:
            draw_main()
            try:
                choice = input("").strip()
            except (KeyboardInterrupt, EOFError):
                break

            if choice == "0":
                break
            elif choice == "1":
                monitor_realtime()
                continue
            elif choice == "2":
                show_log()
            elif choice == "3":
                toggle_ac()
            elif choice == "4":
                restart_service("bluetti", "Bluetti service",
                                "BLE putus sesaat")
            elif choice == "5":
                restart_service("automation", "Automation",
                                "rule berhenti sesaat")
            elif choice == "6":
                pause_automation()
            elif choice == "7":
                resume_automation()
            else:
                # Input tidak valid — redraw saja
                pass

    finally:
        _refresh_stop.set()
        clear()
        print(dim("  Keluar dari Bluetti Control Menu."))
        print()

if __name__ == "__main__":
    main()
