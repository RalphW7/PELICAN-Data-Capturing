# Capture rig software

`capture.py` records the dataset for the landing-zone model. A button or switch on a GPIO pin starts and stops recording, and each recording becomes one session folder.

Files:

- `capture.py`: the capture service
- `config.yaml`: settings (fill in the `FILL IN` values before first use)
- `capture.service`: systemd unit so it starts at boot

## 1. Install dependencies

First run the Rubik Pi setup script from the camera documentation page, which installs Qualcomm's camera packages, then reboot. After that:

```
sudo apt install -y python3-yaml python3-libgpiod gpiod ffmpeg gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
sudo pip install --break-system-packages pymavlink
```

`pymavlink` is only needed if telemetry is enabled. The script works with both the v1 and v2 `gpiod` Python bindings.

## Important: run with sudo

The Rubik Pi camera service only gives full camera access to root. As a regular user, the camera reports no usable formats and every pipeline fails with "can't handle caps." The script therefore refuses to start with the real camera unless it's run as root, and the systemd service runs as root. Session files are handed to the user named in `file_owner` (default `ubuntu`) so you can copy and delete them without `sudo`.

## 2. Wire the button (and optional LED)

The header uses 3.3 V logic. Never connect 5 V to a GPIO pin.

- **Push button:** one leg to a GPIO pin, the other to a GND pin. The script enables the internal pull-up resistor, so pressing the button pulls the pin low (`active_low: true`). If the pull-up isn't supported on your pin, add an external 10 kΩ resistor from the pin to 3.3 V.
- **Toggle switch instead:** same wiring, with `mode: level`. Recording runs while the switch is on, including if it's already on at boot.
- **LED:** GPIO pin to a 330 Ω resistor, then the LED's anode (long leg); the cathode goes to GND. Patterns: a short blip every 2 s means idle, solid means recording, fast blinking means an error.

## 3. Find the GPIO chip and line

Linux addresses GPIOs as a chip plus a line offset, not by header pin number. Use the Rubik Pi pinout documentation to find which chip and line your header pin maps to, then confirm with:

```
sudo gpioinfo                 # lists every chip and line
sudo gpiomon <chip> <line>    # prints events as you press the button
```

(The GPIO devices are root-only by default, hence `sudo`.)

(Command syntax differs slightly between libgpiod tool versions; check `gpiomon --help`.) Put the values into `button.chip` and `button.line`.

## 4. Camera settings

The defaults match what worked during bring-up with the Camera Module 3 (IMX708):

- **Frame rate 14 fps.** The camera service offers at most 14 fps for this camera. Asking for more (the docs' examples use 30) fails with "can't handle caps."
- **Camera index 0.** The index counts *detected* cameras from 0. With one camera connected it's 0, whichever connector it's in. The docs' "CAM1 = 0, CAM2 = 1" only applies with both connectors in use.
- **1920×1080**, which is 16:9 like the sensor, so the image is scaled, not cropped.
- **Encoder `auto`.** At startup the script picks the first hardware encoder it finds: `qtic2venc` (Qualcomm's), then `v4l2h264enc`. The log line "Using hardware encoder: …" shows which. To force one, name it in `camera.encoder`.
- **Extra camera settings** (exposure, white balance, and so on) go in `camera.props` as `name=value` pairs. List the available ones with `sudo gst-inspect-1.0 qtiqmmfsrc`, and test a change with a short `gst-launch-1.0` capture before relying on it.

Quick manual check that the camera works (Ctrl+C after a few seconds):

```
sudo gst-launch-1.0 -e qtiqmmfsrc camera=0 ! video/x-raw,format=NV12,width=1280,height=720,framerate=14/1 ! queue ! jpegenc ! queue ! multifilesink location=/home/ubuntu/img0_%d.jpg max-files=5
```

Autofocus isn't supported for this camera on the Rubik Pi, so check that test images are sharp at the distances you'll capture from.

Segments split at the first keyframe after `segment_seconds`, so if the encoder's keyframe interval is long, segments will run a little longer. That's harmless.

## 5. Test

Without any hardware (works on a laptop too; uses a test pattern and a software encoder, no sudo needed):

```
python3 capture.py --config config.yaml --sim-input --test-source
```

Press Enter to start and stop, then `q` and Enter to quit.

With the real camera but no button wired yet:

```
sudo python3 capture.py --config config.yaml --sim-input
```

With everything connected, drop `--sim-input` and use the button.

## 6. Run at boot

```
sudo mkdir -p /opt/capture
sudo cp capture.py config.yaml /opt/capture/
sudo cp capture.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now capture
journalctl -u capture -f      # live log
```

Stopping the service (`systemctl stop capture`) finalizes the open recording cleanly before exiting.

## Session folder contents

```
session_0007_20261031-142233/
  session.json     settings, pipeline, start/stop times, file list, empty "notes" field
  video_00000.mkv  video mode: one file per segment
  frame_000000.jpg jpeg mode: one file per frame
  frames.csv       jpeg mode: write time of each frame
  telemetry.csv    if telemetry is enabled
  gstreamer.log    pipeline output; check here first if something fails
```

Fill in the `notes` field in `session.json` after each session (location, lighting, box layout, floor type). It makes the later session-based train/validation/test split much easier.

## Copying sessions to your PC

Run this on the PC, not in an SSH session on the Pi. Check the prompt: `ubuntu@ubuntu` means you're on the Pi. In PowerShell on Windows:

```
scp -r ubuntu@<pi-address>:~/captures/session_0007_* $HOME\Downloads\
```

On Mac or Linux, use `~/Downloads/` as the destination. To copy everything at once, use `~/captures/*`.

## Troubleshooting

The terminal fills with "Broadcast message from systemd-journald" lines from the camera service during every capture. Almost all are routine; the ones below are not.

- **"can't handle caps"**: not running as root, or a resolution or frame rate the camera doesn't offer. `sudo gst-inspect-1.0 qtiqmmfsrc` lists the real limits.
- **"Invalid camera idx"**: wrong `camera.index`; with one camera it should be 0.
- **Camera not detected at all**: `sudo journalctl -k | grep -i "probe success"` should show the sensor (IMX708 appears as `sensor_id:0x708`). If not, power off and reseat the cable with the connector locks closed.
- **Pipeline fails when recording starts**: the script copies the end of `gstreamer.log` into its own log, so `journalctl -u capture` shows the actual GStreamer error.

To stop the broadcast messages printing to the terminal (they still go to the system log):

```
sudo sed -i 's/^#\?ForwardToWall=.*/ForwardToWall=no/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

## Power-loss behavior

Video is written as short .mkv segments. If power is cut, finished segments are intact and the open segment is still readable up to roughly the last second. In JPEG mode, at most the frame being written is lost. If the pipeline dies on its own, the script logs the error, records `"crashed": true` in `session.json`, and switches the LED to error. Pressing the button starts a fresh session.

## Telemetry (optional)

1. Configure a flight-controller serial port for MAVLink at 921600 baud. On ArduPilot that's `SERIALn_PROTOCOL = 2` and `SERIALn_BAUD = 921`; on PX4, the `MAV_n_CONFIG` and serial baud parameters. Check your firmware's docs for the exact names.
2. Wire flight-controller TX to Pi RX, RX to TX, and GND to GND. Don't power the Pi from this port.
3. Set `telemetry.enabled: true` and `telemetry.device` to the Pi's UART device.

While recording, the script logs attitude, local and global position, altitude (VFR_HUD), rangefinder distance, and heartbeat (which includes armed state) to `telemetry.csv`, and sends "Capture started" or "Capture stopped" text to your ground control station.

## Matching frames to telemetry

Both use the Pi's wall clock.

- **JPEG mode:** use `write_time_epoch` from `frames.csv`. It's the time the file was written, typically within a few tens of milliseconds of capture.
- **Video mode:** frame time is approximately `start_time_epoch` plus the frame's timestamp within the recording, plus the time earlier segments cover. The camera takes a fraction of a second to start, so this has a small constant offset. For precise alignment, use JPEG mode, or measure the offset once by filming a clock or a sharp event like an LED switching on.
