# Bluetti Pi Automation

Device  : Bluetti Elite 200 V2 (PR200V2)
Pi      : Raspberry Pi 3B+
Stack   : Pi OS Lite + Mosquitto + desalvo + automation.py + menu.py + Tailscale

## Restore ke SD baru:
1. Flash Pi OS Lite (SSH aktif, WiFi dikonfigurasi)
2. SSH masuk
3. git clone [repo-url]
4. cd bluetti-project
5. ./install.sh
6. Edit MAC di bluetti.service
7. sudo systemctl start bluetti automation

## Files:
- automation.py   : 7 rule UPS automation
- menu.py         : SSH terminal menu (alias: menu)
- bluetti.service : systemd desalvo service
- install.sh      : script install otomatis (dibuat terpisah)
- README.md       : panduan ini
