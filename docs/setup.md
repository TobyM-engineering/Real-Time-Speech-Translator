# Setup Guide

The full build, start to finish. The README has a condensed version — this one includes the parts that are easy to get wrong.

---

## 1. Parts

| Part | Notes |
|------|-------|
| Raspberry Pi Zero 2 W | 512MB RAM. Add a heatsink. |
| microSD card (32GB) | Class 10 or better |
| ReSpeaker Lite (USB) | Has onboard noise suppression and beamforming, which matters a lot for a device that listens to a room |
| USB Bluetooth adapter | See the warning in step 4 |
| USB hub HAT | The Pi Zero has one usable USB port; you need at least two |
| UPS HAT + battery | For portable power |
| RGB LED (common cathode) | Plus 3 × 330Ω resistors |
| Momentary push button | No resistor needed |

Total is roughly $65.

---

## 2. Operating system

Flash **Raspberry Pi OS (64-bit)** to the SD card. Use the Raspberry Pi Imager and, in its advanced settings, pre-configure:

- Hostname
- SSH enabled with a username and password
- Your WiFi network

That gets you a Pi you can reach over SSH without ever attaching a monitor.

### Disable the desktop environment

This matters more than it sounds. On a 512MB board, the graphical desktop consumes a large share of available RAM and directly caused audio stuttering and swap thrashing during development.

```bash
sudo systemctl set-default multi-user.target
sudo reboot
```

Check the result with `free -h`. Swap usage should be near zero at idle.

---

## 3. Software dependencies

```bash
sudo apt update
sudo apt install -y python3-venv swig python3-dev liblgpio-dev
```

`swig`, `python3-dev`, and `liblgpio-dev` are required to build `lgpio`, which `gpiozero` needs to talk to the GPIO pins. Without them the install fails partway through with a linker error.

```bash
cd ~
git clone https://github.com/TobyM-engineering/Real-Time-Speech-Translator.git translator
cd translator
python3 -m venv .venv
source .venv/bin/activate
pip install websocket-client python-dotenv gpiozero lgpio
```

---

## 4. Bluetooth

### Disable the onboard radio

The Pi Zero 2 W shares one antenna between WiFi and Bluetooth. Streaming audio out over Bluetooth while streaming audio up over WiFi makes them compete, which produces constant dropouts. Moving Bluetooth to a separate USB adapter and turning the onboard radio off fixed this completely.

```bash
echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

After rebooting, `bluetoothctl list` should show only your USB adapter.

### Check for a soft block

A fresh adapter often comes up blocked, which looks like a dead device:

```bash
rfkill list
sudo rfkill unblock bluetooth
```

### Disable USB autosuspend

Power management putting the adapter to sleep mid-session causes disconnects:

```bash
echo 'ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="2357", ATTR{idProduct}=="0604", TEST=="power/control", ATTR{power/control}="on"' | sudo tee /etc/udev/rules.d/99-bluetooth-no-suspend.conf
sudo chmod 440 /etc/udev/rules.d/99-bluetooth-no-suspend.conf
sudo udevadm control --reload-rules
```

Replace the vendor and product IDs with your adapter's, which you can find with `lsusb`.

### Pair your earbuds

Run each command individually and wait for its response. Pasting them together does not work reliably — `bluetoothctl` is interactive and drops queued input.

```bash
bluetoothctl
power on
scan on
```

Wait for your device to appear, then:

```bash
trust XX:XX:XX:XX:XX:XX
pair XX:XX:XX:XX:XX:XX
connect XX:XX:XX:XX:XX:XX
exit
```

Confirm the audio system can see it:

```bash
wpctl status
```

Your earbuds should be listed under **Sinks**.

### A note on adapter choice

Not all Bluetooth adapters are equal under sustained load. The RTL8761B-based adapter used in this build crashes intermittently during long sessions, which shows up in `dmesg` as:

```
Bluetooth: hci0: RTL: hw err, trigger devcoredump
```

The software includes recovery logic for this (see `how_it_works.md`), but a more stable adapter is the better fix. If you are buying one, look for reviews specifically mentioning Linux or Raspberry Pi audio.

---

## 5. Wiring

See `hardware/wiring_diagram.png` for the full diagram.

| Component | GPIO (BCM) | Physical Pin |
|-----------|-----------|--------------|
| Button | GPIO17 | Pin 11 |
| Button GND | — | Pin 9 |
| LED Red | GPIO27 | Pin 13 |
| LED Green | GPIO22 | Pin 15 |
| LED Blue | GPIO23 | Pin 16 |
| LED GND | — | Pin 14 |

Each LED color leg needs a 330Ω resistor. The button needs none — the code uses the Pi's internal pull-up.

**Avoid GPIO2 and GPIO3 (physical pins 3 and 5).** The UPS HAT uses those for I²C battery monitoring.

If your LED is common anode instead of common cathode, wire the common leg to 3.3V (pin 1) and set `LED_ACTIVE_HIGH = False` in `translator.py`.

Test the wiring before going further:

Pressing the button should cycle the LED through its colors.

---

## 6. Configuration

Create a `.env` file in the project folder:

```
OPENAI_API_KEY=your_key_here
AIRPODS_MAC=XX:XX:XX:XX:XX:XX
AIRPODS_NAME=AirPods Pro
```

`AIRPODS_NAME` must match how the device appears in `wpctl status`, since that is how the code locates the audio sink.

You need an **OpenAI API account with credits**, which is separate from a ChatGPT subscription. Add credits at platform.openai.com under Billing. Realtime audio models cost noticeably more per minute than text, so watch usage during long sessions.

> Never commit `.env`. It is already in `.gitignore`.

---

## 7. First run

```bash
source .venv/bin/activate
python3 software/translator.py
```

Watch the LED move through blinking blue → blinking magenta → white → green. Once it is green, play or speak a foreign language near the microphone.

---

## 8. Run automatically on boot

```bash
mkdir -p ~/.config/systemd/user
cp docs/translator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable translator.service
systemctl --user start translator.service
```

### Enable lingering

Without this, the service will not start until you log in, which defeats the purpose of a headless device:

```bash
sudo loginctl enable-linger $USER
```

### Make sure the audio services start too

With the desktop disabled, PipeWire may not start on its own:

```bash
systemctl --user enable pipewire.service pipewire-pulse.service wireplumber.service
```

### Allow the recovery logic to work

The software restarts the Bluetooth service when the adapter wedges, which needs permission to run without a password prompt:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart bluetooth" | sudo tee /etc/sudoers.d/translator-bluetooth
sudo chmod 440 /etc/sudoers.d/translator-bluetooth
```

---

## 9. Checking on it

```bash
systemctl --user status translator.service
tail -f ~/translator_debug.log
```

The log records every phrase with its source text, translation, and whether it was muted, which makes it possible to verify the language filter is behaving.

---

## Troubleshooting

**No Bluetooth controller found**
`bluetoothctl list` returns nothing. Usually `bluetoothd` started before the USB adapter finished enumerating. `sudo systemctl restart bluetooth` fixes it. The software handles this automatically on startup.

**Paired but no audio output**
The device connects but no sink appears in `wpctl status`. Check that PipeWire and WirePlumber are actually running — with the desktop disabled they may not auto-start.

**Audio cuts out or lags badly**
Check `free -h` for swap usage and `dmesg | grep -i bluetooth` for adapter crashes. On this hardware the usual causes are memory pressure or the adapter itself.

**Microphone not detected**
`arecord -l` should list the ReSpeaker. If not, reseat the USB connection. The ReSpeaker can wedge in a state where only a physical replug recovers it.

**Everything worked, then stopped**
Confirm your OpenAI account still has credits. When it runs out, the connection is rejected and every phrase logs with empty text, which looks like a code problem but is not.
