# Raspberry Pi Deployment

These steps deploy Loggerhead to the Pi shown in the setup notes:

```text
reef@192.168.60.3
working directory: /home/reef/controller
```

## 1. Connect

```powershell
ssh reef@192.168.60.3
```

Then on the Pi:

```bash
cd ~/controller
```

## 2. Install

Run the installer directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/RealBlueSky227/loggerhead/main/scripts/install_pi.sh | bash
```

The installer clones or updates `~/controller/loggerhead`, creates `~/controller/loggerhead/.venv`, installs dependencies, creates the first safe config, installs `systemd/loggerhead.service`, enables `pigpiod`, and enables the Loggerhead service.

## 3. Review Config Before Hardware Start

```bash
nano ~/controller/loggerhead/config/loggerhead.json
```

Loggerhead enforces the fixed SRS hardware map. If a pin, relay, pH address, Kasa outlet, stepper assignment, or buzzer pin is outside the allowed map, startup halts before driving hardware.

## 4. Start

```bash
sudo systemctl start loggerhead
sudo systemctl status loggerhead
journalctl -u loggerhead -f
```

Dashboard:

```text
http://192.168.60.3:8080
```

## Update Later

```bash
cd ~/controller/loggerhead
git pull --ff-only
. .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl restart loggerhead
```

## Useful Diagnostics

```bash
systemctl status pigpiod
i2cdetect -y 1
journalctl -u loggerhead -n 100 --no-pager
```

If `python3.13` is missing, install Python 3.13 for Raspberry Pi OS first. Loggerhead targets Python 3.13 to match the SRS.
