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

The installer clones or updates `~/controller/loggerhead`, creates `~/controller/loggerhead/.venv`, installs dependencies, creates the first safe config, installs `systemd/loggerhead.service`, installs the restricted DS18B20 `loggerhead-onewire-helper`, enables `pigpiod`, and enables the Loggerhead service.

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

## Dynamic DS18B20 Kernel 1-Wire

Loggerhead uses the Raspberry Pi kernel `w1-gpio` driver for DS18B20 timing. The web UI identifies probes by physical SensePort only; ROM IDs are diagnostics, not configuration.

The runtime helper is installed as:

```bash
/usr/local/sbin/loggerhead-onewire-helper
/etc/sudoers.d/loggerhead-onewire-helper
```

Verify the restricted helper path:

```bash
sudo -n /usr/local/sbin/loggerhead-onewire-helper status
```

Manual GPIO17 validation, matching the October 11, 2026 hardware test:

```bash
sudo -n /usr/local/sbin/loggerhead-onewire-helper activate 17
dtoverlay -l
gpioinfo gpiochip0 | grep GPIO17
cat /sys/bus/w1/devices/w1_bus_master*/w1_master_slaves
cat /sys/bus/w1/devices/28-*/w1_slave
cat /sys/bus/w1/devices/28-*/ext_power
cat /sys/bus/w1/devices/28-*/resolution
sudo -n /usr/local/sbin/loggerhead-onewire-helper release 17
```

Migration from boot-time overlays:

1. Back up `/boot/firmware/config.txt`.
2. Inspect existing `dtoverlay=w1-gpio,gpiopin=...` lines and current consumers with `gpioinfo gpiochip0`.
3. Comment out the boot-time Loggerhead SensePort `w1-gpio` overlays.
4. Reboot once.
5. Confirm `dtoverlay -l` does not list those boot overlays and that SensePort GPIOs are not already owned by another driver.
6. Start Loggerhead and use the dashboard to assign DS18B20 sensors by SensePort.

Rollback is the reverse: stop Loggerhead, restore the backed-up `config.txt`, reboot, and remove `/etc/sudoers.d/loggerhead-onewire-helper` if dynamic management is no longer wanted.

Important limitation: Raspberry Pi runtime overlays are stack-managed. Removing an earlier runtime overlay can temporarily remove and reapply later overlays. Loggerhead serializes helper operations and marks affected sensor readings stale, but independent multiport runtime removal and the post-removal electrical state of each GPIO must still be validated on the actual Pi before relying on it for unattended operation.

## Useful Diagnostics

```bash
systemctl status pigpiod
i2cdetect -y 1
journalctl -u loggerhead -n 100 --no-pager
sudo -n /usr/local/sbin/loggerhead-onewire-helper status
dtoverlay -l
gpioinfo gpiochip0
```

If `python3.13` is missing, install Python 3.13 for Raspberry Pi OS first. Loggerhead targets Python 3.13 to match the SRS.
