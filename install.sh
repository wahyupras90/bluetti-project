#!/bin/bash
# install.sh — Bluetti Pi Automation
# Jalankan setelah fresh Pi OS Lite + SSH masuk

set -e

echo "=== Install dependency ==="
sudo apt update
sudo apt install -y git python3 python3-venv mosquitto mosquitto-clients bluetooth bluez

echo "=== Buat venv ==="
python3 -m venv ~/bluetti-desalvo
source ~/bluetti-desalvo/bin/activate

echo "=== Install Python library ==="
pip install --upgrade pip
pip install cryptography pyasn1 prometheus_client bleak paho-mqtt
pip install git+https://github.com/desalvo/bluetti_mqtt.git@v2devices

echo "=== Patch PR200V2 ==="
python3 - << 'PY'
import os
import bluetti_mqtt.bluetooth as bt
path = os.path.join(os.path.dirname(bt.__file__), '__init__.py')
with open(path, 'r') as f:
    c = f.read()
if 'PR200V2' not in c:
    c = c.replace("EL400)", "EL400|PR200V2)")
    c = c.replace(
        "    return None",
        "    if match[1] == 'PR200V2':\n        return V2Device(address, match[2], 'PR200V2')\n    return None"
    )
    with open(path, 'w') as f:
        f.write(c)
    print('PR200V2 patch OK')
else:
    print('PR200V2 sudah ada, skip')
PY

echo "=== Copy file ==="
cp automation.py ~/automation.py
cp menu.py ~/menu.py
sudo cp bluetti.service /etc/systemd/system/

echo "=== Setup systemd ==="
sudo systemctl daemon-reload
sudo systemctl enable mosquitto
sudo systemctl enable bluetti
sudo systemctl start mosquitto

echo "=== Setup alias menu ==="
grep -q "alias menu=" ~/.bashrc || \
  echo "alias menu='source ~/bluetti-desalvo/bin/activate && python3 ~/menu.py'" >> ~/.bashrc

echo ""
echo "=== SELESAI ==="
echo "Langkah berikutnya:"
echo "1. Edit MAC di: sudo nano /etc/systemd/system/bluetti.service"
echo "2. sudo systemctl start bluetti"
echo "3. source ~/.bashrc && menu"
