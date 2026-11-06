#!/usr/bin/env python3
"""
Image/video capture service for the landing-zone dataset rig (Rubik Pi 3).

Start/stop comes from a GPIO input (push button or toggle switch).
Recording runs as a GStreamer subprocess so the hardware encoder does the work.

Modes
  video : segmented H.264 in .mkv files (survives sudden power loss)
  jpeg  : still frames at a fixed interval

Each press of the button creates one session folder:
  <output_root>/session_0007_20261031-142233/
      session.json      config, pipeline, start/stop times, file list
      video_00000.mkv   (video mode)  or  frame_000000.jpg ... (jpeg mode)
      frames.csv        (jpeg mode) file write time for each frame
      telemetry.csv     (optional) MAVLink messages logged during the session
      gstreamer.log     pipeline output, useful for debugging

The Rubik Pi camera service only gives full camera access to root, so run
this with sudo (the systemd service already runs as root).

Testing without hardware (no sudo needed):
  python3 capture.py --config config.yaml --sim-input --test-source
  (press Enter to start/stop, 'q' + Enter to quit)

Testing with the real camera, keyboard instead of the button:
  sudo python3 capture.py --config config.yaml --sim-input
"""

import argparse
import copy
import csv
import json
import os
import logging
import queue
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

log = logging.getLogger("capture")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "output_root": "/home/ubuntu/captures",
    "file_owner": "ubuntu",  # chown sessions to this user so they're easy to copy/delete; null to skip
    "mode": "video",  # "video" or "jpeg"
    "camera": {
        "width": 1920,
        "height": 1080,
        "fps": 14,       # Camera Module 3 (IMX708) on Rubik Pi offers at most 14 fps
        # Index counts *detected* cameras from 0, not connector labels:
        # a single camera is index 0 whichever connector it's in.
        "index": 0,
        "props": "",     # extra qtiqmmfsrc settings, e.g. exposure/white balance ("name=value ...")
        "source": ("qtiqmmfsrc name=camsrc camera={index} {props} ! "
                   "video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1"),
        "encoder": "auto",  # "auto" picks the first available of ENCODER_CANDIDATES
    },
    "video": {"segment_seconds": 60, "muxer": "matroskamux", "extension": "mkv"},
    "jpeg": {"interval_s": 0.5, "quality": 90},
    "button": {
        "chip": None,          # e.g. "/dev/gpiochip4"  (find with `gpioinfo`)
        "line": None,          # line offset on that chip, NOT the header pin number
        "active_low": True,    # True when the button connects the pin to GND
        "bias": "pull_up",     # "pull_up", "pull_down", "disabled", "as_is"
        "mode": "toggle",      # "toggle" = momentary button, "level" = latching switch
        "debounce_ms": 30,
        "long_press_s": 3.0,   # toggle mode only; set null to disable
        "long_press_action": "none",  # "none" or "shutdown"
    },
    "led": {"enabled": False, "chip": None, "line": None, "active_low": False},
    "storage": {"min_free_gb": 2.0, "check_interval_s": 5.0},
    "telemetry": {
        "enabled": False,
        "device": None,        # e.g. "/dev/ttyHS1"; check the Rubik Pi UART docs
        "baud": 921600,
        "rate_hz": 20,
        "source_system": 1,    # usually matches the flight controller's system id
        "status_text": True,   # send "capture started/stopped" to the ground station
    },
}


# Hardware H.264 encoders to try, in order, when camera.encoder is "auto".
ENCODER_CANDIDATES = ["qtic2venc", "v4l2h264enc"]


def gst_element_exists(name):
    try:
        return subprocess.run(["gst-inspect-1.0", name], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_encoder(setting):
    if setting != "auto":
        return setting
    for name in ENCODER_CANDIDATES:
        if gst_element_exists(name):
            log.info("Using hardware encoder: %s", name)
            return name
    sys.exit("No hardware H.264 encoder found (tried: %s). Set camera.encoder in the config, "
             "or use mode: jpeg." % ", ".join(ENCODER_CANDIDATES))


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path):
    if path is None:
        return copy.deepcopy(DEFAULT_CONFIG)
    if yaml is None:
        sys.exit("PyYAML is required to read the config file: pip install pyyaml")
    with open(path) as f:
        return deep_merge(DEFAULT_CONFIG, yaml.safe_load(f) or {})


# --------------------------------------------------------------------------
# GPIO (supports libgpiod Python bindings v1.x and v2.x)
# --------------------------------------------------------------------------

def _require_line(section, name):
    if section.get("chip") is None or section.get("line") is None:
        sys.exit(f"Config error: set {name}.chip and {name}.line. "
                 "Run `gpioinfo` and check the Rubik Pi pinout to find them.")


class GpioLine:
    """Single GPIO line as input or output, hiding libgpiod version differences."""

    def __init__(self, chip, line, output=False, active_low=False, bias="as_is"):
        import gpiod
        self.line = int(line)
        self.v2 = hasattr(gpiod, "request_lines")
        if self.v2:
            from gpiod.line import Bias, Direction, Value
            self._Value = Value
            bias_map = {"pull_up": Bias.PULL_UP, "pull_down": Bias.PULL_DOWN,
                        "disabled": Bias.DISABLED, "as_is": Bias.AS_IS}
            settings = gpiod.LineSettings(
                direction=Direction.OUTPUT if output else Direction.INPUT,
                active_low=active_low,
                bias=Bias.AS_IS if output else bias_map[bias],
            )
            self.req = gpiod.request_lines(chip, consumer="capture",
                                           config={self.line: settings})
        else:
            flags = 0
            if active_low:
                flags |= gpiod.LINE_REQ_FLAG_ACTIVE_LOW
            if not output:
                flags |= {"pull_up": getattr(gpiod, "LINE_REQ_FLAG_BIAS_PULL_UP", 0),
                          "pull_down": getattr(gpiod, "LINE_REQ_FLAG_BIAS_PULL_DOWN", 0),
                          "disabled": getattr(gpiod, "LINE_REQ_FLAG_BIAS_DISABLE", 0),
                          "as_is": 0}[bias]
            self.chip = gpiod.Chip(chip)
            self.req = self.chip.get_line(self.line)
            self.req.request(consumer="capture",
                             type=gpiod.LINE_REQ_DIR_OUT if output else gpiod.LINE_REQ_DIR_IN,
                             flags=flags)

    def read(self):
        if self.v2:
            return self.req.get_value(self.line) == self._Value.ACTIVE
        return bool(self.req.get_value())

    def write(self, on):
        if self.v2:
            self.req.set_value(self.line, self._Value.ACTIVE if on else self._Value.INACTIVE)
        else:
            self.req.set_value(1 if on else 0)

    def close(self):
        try:
            self.req.release()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Inputs: they push events into a queue for the main loop
#   "toggle", "level_on", "level_off", "long_press", "quit"
# --------------------------------------------------------------------------

class ButtonMonitor(threading.Thread):
    POLL_S = 0.005

    def __init__(self, cfg, events):
        super().__init__(daemon=True)
        _require_line(cfg, "button")
        self.cfg = cfg
        self.events = events
        self.running = True
        self.gpio = GpioLine(cfg["chip"], cfg["line"], output=False,
                             active_low=cfg["active_low"], bias=cfg["bias"])

    def run(self):
        debounce = self.cfg["debounce_ms"] / 1000.0
        long_s = self.cfg.get("long_press_s")
        level_mode = self.cfg["mode"] == "level"

        stable = self.gpio.read()
        candidate, candidate_since = stable, time.monotonic()
        pressed_at, long_fired = None, False

        if level_mode:  # honour the switch position at boot
            self.events.put("level_on" if stable else "level_off")

        while self.running:
            raw = self.gpio.read()
            now = time.monotonic()
            if raw != candidate:
                candidate, candidate_since = raw, now
            elif raw != stable and now - candidate_since >= debounce:
                stable = raw
                if level_mode:
                    self.events.put("level_on" if stable else "level_off")
                elif stable:  # pressed
                    pressed_at, long_fired = now, False
                    if not long_s:
                        self.events.put("toggle")
                else:  # released
                    if long_s and not long_fired:
                        self.events.put("toggle")
                    pressed_at = None

            if (not level_mode and long_s and pressed_at is not None
                    and not long_fired and now - pressed_at >= long_s):
                long_fired = True
                self.events.put("long_press")
            time.sleep(self.POLL_S)

    def close(self):
        self.running = False
        self.gpio.close()


class SimInput(threading.Thread):
    """Keyboard stand-in for the button: Enter toggles, 'q' quits."""

    def __init__(self, events):
        super().__init__(daemon=True)
        self.events = events

    def run(self):
        print("Simulated button: press Enter to start/stop, 'q' + Enter to quit.")
        for line in sys.stdin:
            self.events.put("quit" if line.strip().lower() == "q" else "toggle")
        self.events.put("quit")

    def close(self):
        pass


# --------------------------------------------------------------------------
# Status LED
# --------------------------------------------------------------------------

class StatusLed(threading.Thread):
    """idle: short blip every 2 s | recording: solid | error: fast blink."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        _require_line(cfg, "led")
        self.gpio = GpioLine(cfg["chip"], cfg["line"], output=True,
                             active_low=cfg["active_low"])
        self.state = "idle"
        self.running = True

    def run(self):
        while self.running:
            t = time.monotonic()
            if self.state == "recording":
                on = True
            elif self.state == "error":
                on = (t % 0.25) < 0.125
            else:
                on = (t % 2.0) < 0.08
            self.gpio.write(on)
            time.sleep(0.02)

    def close(self):
        self.running = False
        time.sleep(0.05)
        self.gpio.write(False)
        self.gpio.close()


# --------------------------------------------------------------------------
# Optional MAVLink telemetry logging
# --------------------------------------------------------------------------

class TelemetryLogger(threading.Thread):
    MESSAGES = ["ATTITUDE", "LOCAL_POSITION_NED", "GLOBAL_POSITION_INT",
                "VFR_HUD", "DISTANCE_SENSOR", "HEARTBEAT"]

    def __init__(self, cfg):
        super().__init__(daemon=True)
        if not cfg.get("device"):
            sys.exit("Config error: telemetry.enabled is true but telemetry.device is not set.")
        self.cfg = cfg
        self.running = True
        self.conn = None
        self.file_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.csv_file = None
        self.writer = None

    def _connect(self):
        from pymavlink import mavutil
        self.mavutil = mavutil
        conn = mavutil.mavlink_connection(
            self.cfg["device"], baud=self.cfg["baud"],
            source_system=self.cfg["source_system"],
            source_component=mavutil.mavlink.MAV_COMP_ID_ONBOARD_COMPUTER)
        if conn.wait_heartbeat(timeout=10) is None:
            conn.close()
            raise TimeoutError("no heartbeat from flight controller")
        interval_us = int(1e6 / self.cfg["rate_hz"])
        for name in self.MESSAGES[:-1]:
            msg_id = getattr(mavutil.mavlink, f"MAVLINK_MSG_ID_{name}")
            conn.mav.command_long_send(
                conn.target_system, conn.target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                msg_id, interval_us, 0, 0, 0, 0, 0)
        log.info("Telemetry connected (system %d)", conn.target_system)
        return conn

    def run(self):
        while self.running:
            try:
                if self.conn is None:
                    self.conn = self._connect()
                msg = self.conn.recv_match(type=self.MESSAGES, blocking=True, timeout=1.0)
                if msg is None:
                    continue
                with self.file_lock:
                    if self.writer:
                        self.writer.writerow([f"{time.time():.6f}", f"{time.monotonic():.6f}",
                                              msg.get_type(), json.dumps(msg.to_dict())])
            except Exception as exc:
                log.warning("Telemetry link problem (%s); retrying in 2 s", exc)
                try:
                    if self.conn:
                        self.conn.close()
                except Exception:
                    pass
                self.conn = None
                time.sleep(2)

    def start_session(self, session_dir):
        with self.file_lock:
            self.csv_file = open(session_dir / "telemetry.csv", "w", newline="")
            self.writer = csv.writer(self.csv_file)
            self.writer.writerow(["wall_time", "monotonic_time", "msg_type", "data_json"])

    def end_session(self):
        with self.file_lock:
            if self.csv_file:
                self.csv_file.close()
            self.csv_file, self.writer = None, None

    def status_text(self, text):
        if not (self.cfg.get("status_text") and self.conn):
            return
        try:
            with self.send_lock:
                self.conn.mav.statustext_send(
                    self.mavutil.mavlink.MAV_SEVERITY_INFO, text.encode()[:50])
        except Exception as exc:
            log.warning("Could not send status text: %s", exc)

    def close(self):
        self.running = False
        self.end_session()


# --------------------------------------------------------------------------
# Recorder: owns the GStreamer subprocess and the session folder
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, cfg, test_source=False):
        self.cfg = cfg
        self.test_source = test_source
        self.root = Path(cfg["output_root"]).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.session_dir = None
        self.meta = None
        self.gst_log = None
        self.encoder = None
        if cfg["mode"] == "video" and not test_source:
            self.encoder = resolve_encoder(cfg["camera"]["encoder"])

    @property
    def recording(self):
        return self.proc is not None

    def free_gb(self):
        return shutil.disk_usage(self.root).free / 1e9

    def _next_session_dir(self):
        indices = []
        for p in self.root.glob("session_*"):
            try:
                indices.append(int(p.name.split("_")[1]))
            except (IndexError, ValueError):
                pass
        idx = max(indices, default=0) + 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.root / f"session_{idx:04d}_{stamp}"
        path.mkdir()
        return path

    def build_pipeline(self, outdir):
        cam = self.cfg["camera"]
        fmt = {"width": cam["width"], "height": cam["height"], "fps": cam["fps"],
               "index": cam.get("index", 0), "props": cam.get("props") or ""}
        if self.test_source:
            source = ("videotestsrc is-live=true pattern=ball ! "
                      "video/x-raw,width={width},height={height},framerate={fps}/1").format(**fmt)
            encoder = f"x264enc tune=zerolatency speed-preset=ultrafast key-int-max={cam['fps']}"
        else:
            source = " ".join(cam["source"].format(**fmt).split())  # tidy spaces if props is empty
            encoder = self.encoder

        if self.cfg["mode"] == "video":
            v = self.cfg["video"]
            seg_ns = int(v["segment_seconds"] * 1e9)
            location = outdir / f"video_%05d.{v['extension']}"
            return (f"{source} ! queue ! {encoder} ! h264parse ! "
                    f"splitmuxsink muxer-factory={v['muxer']} "
                    f"location={location} max-size-time={seg_ns}")

        if self.cfg["mode"] == "jpeg":
            j = self.cfg["jpeg"]
            rate = Fraction(1.0 / j["interval_s"]).limit_denominator(1000)
            location = outdir / "frame_%06d.jpg"
            return (f"{source} ! queue leaky=downstream max-size-buffers=2 ! "
                    f"videorate drop-only=true ! "
                    f"video/x-raw,framerate={rate.numerator}/{rate.denominator} ! "
                    f"videoconvert ! jpegenc quality={j['quality']} ! "
                    f"multifilesink location={location}")

        raise ValueError(f"unknown mode: {self.cfg['mode']}")

    def _chown(self, path):
        """Hand files to file_owner (and that user's primary group) when running as root."""
        owner = self.cfg.get("file_owner")
        if not owner or os.geteuid() != 0 or getattr(self, "_chown_failed", False):
            return
        try:
            import pwd
            entry = pwd.getpwnam(owner)
            os.chown(path, entry.pw_uid, entry.pw_gid)
        except (KeyError, OSError) as exc:
            log.warning("Could not give files to user '%s' (%s); leaving them owned by root",
                        owner, exc)
            self._chown_failed = True

    def _write_meta(self):
        with open(self.session_dir / "session.json", "w") as f:
            json.dump(self.meta, f, indent=2)

    def start(self):
        if self.recording:
            return
        free = self.free_gb()
        if free < self.cfg["storage"]["min_free_gb"]:
            raise RuntimeError(f"only {free:.1f} GB free")

        self.session_dir = self._next_session_dir()
        self._chown(self.session_dir)
        pipeline = self.build_pipeline(self.session_dir)
        self.gst_log = open(self.session_dir / "gstreamer.log", "w")
        start_wall = time.time()
        self.proc = subprocess.Popen(
            ["gst-launch-1.0", "-e"] + shlex.split(pipeline),
            stdout=self.gst_log, stderr=subprocess.STDOUT,
            start_new_session=True)  # keep Ctrl+C on the terminal from reaching it directly

        self.meta = {
            "session": self.session_dir.name,
            "hostname": socket.gethostname(),
            "mode": self.cfg["mode"],
            "test_source": self.test_source,
            "start_time_utc": datetime.fromtimestamp(start_wall, timezone.utc).isoformat(),
            "start_time_epoch": start_wall,
            "camera": self.cfg["camera"],
            "encoder": self.encoder,
            "pipeline": pipeline,
            "notes": "",  # fill in afterwards: location, lighting, box layout, floor type
        }
        self._write_meta()
        log.info("Recording started: %s", self.session_dir.name)

    def _log_gst_tail(self, lines=15):
        """Copy the end of gstreamer.log into the main log so failures show up in journalctl."""
        try:
            text = (self.session_dir / "gstreamer.log").read_text(errors="replace")
        except OSError:
            return
        useful = [l for l in text.splitlines()
                  if l.strip() and "Broadcast message" not in l and "MESA" not in l
                  and "failed to get driver name" not in l]
        for line in useful[-lines:]:
            log.error("  gst: %s", line)

    def check_alive(self):
        """Returns an error string if the pipeline died on its own."""
        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            self._finish(exit_code=code, crashed=True)
            return f"pipeline exited unexpectedly (code {code}); see gstreamer.log"
        return None

    def stop(self):
        if not self.recording:
            return
        self.proc.send_signal(signal.SIGINT)  # gst-launch -e turns this into EOS
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("Pipeline did not stop after EOS; terminating")
            self.proc.terminate()
            try:
                code = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                code = self.proc.wait()
        self._finish(exit_code=code, crashed=False)

    def _finish(self, exit_code, crashed):
        stop_wall = time.time()
        if self.gst_log:
            self.gst_log.close()
        files = sorted(p.name for p in self.session_dir.iterdir()
                       if p.suffix in (".mkv", ".mp4", ".jpg"))

        if self.cfg["mode"] == "jpeg":
            with open(self.session_dir / "frames.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["filename", "write_time_epoch"])
                for name in files:
                    w.writerow([name, f"{(self.session_dir / name).stat().st_mtime:.6f}"])

        self.meta.update({
            "stop_time_utc": datetime.fromtimestamp(stop_wall, timezone.utc).isoformat(),
            "stop_time_epoch": stop_wall,
            "duration_s": round(stop_wall - self.meta["start_time_epoch"], 2),
            "exit_code": exit_code,
            "crashed": crashed,
            "files": files,
        })
        self._write_meta()
        for p in self.session_dir.iterdir():
            self._chown(p)
        if crashed:
            self._log_gst_tail()
        log.info("Recording stopped: %s (%d files, %.0f s)",
                 self.session_dir.name, len(files), self.meta["duration_s"])
        self.proc = None


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="YAML config file (overrides built-in defaults)")
    ap.add_argument("--sim-input", action="store_true",
                    help="use the keyboard instead of the GPIO button")
    ap.add_argument("--test-source", action="store_true",
                    help="use a synthetic test pattern and software encoder (no camera needed)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if shutil.which("gst-launch-1.0") is None:
        sys.exit("gst-launch-1.0 not found; install the GStreamer tools.")
    if not args.test_source and os.geteuid() != 0:
        sys.exit("The Rubik Pi camera service needs root access. Run with sudo "
                 "(or use --test-source to test without the camera).")

    events = queue.Queue()
    recorder = Recorder(cfg, test_source=args.test_source)
    inp = SimInput(events) if args.sim_input else ButtonMonitor(cfg["button"], events)
    led = StatusLed(cfg["led"]) if cfg["led"]["enabled"] else None
    telem = TelemetryLogger(cfg["telemetry"]) if cfg["telemetry"]["enabled"] else None

    for worker in (inp, led, telem):
        if worker:
            worker.start()

    stop_requested = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_requested.set())
    signal.signal(signal.SIGINT, lambda *_: stop_requested.set())

    error = False
    last_disk_check = 0.0

    def start_recording():
        nonlocal error
        try:
            recorder.start()
            error = False
            if telem:
                telem.start_session(recorder.session_dir)
                telem.status_text(f"Capture started {recorder.session_dir.name[:12]}")
        except Exception as exc:
            log.error("Could not start recording: %s", exc)
            error = True

    def stop_recording():
        if not recorder.recording:
            return
        if telem:
            telem.end_session()
        recorder.stop()
        if telem:
            telem.status_text("Capture stopped")

    log.info("Ready (mode=%s, output=%s, %.1f GB free)",
             cfg["mode"], recorder.root, recorder.free_gb())

    try:
        while not stop_requested.is_set():
            try:
                ev = events.get(timeout=0.1)
            except queue.Empty:
                ev = None

            if ev == "quit":
                break
            if ev == "toggle":
                stop_recording() if recorder.recording else start_recording()
            elif ev == "level_on" and not recorder.recording:
                start_recording()
            elif ev == "level_off":
                stop_recording()
            elif ev == "long_press":
                if cfg["button"]["long_press_action"] == "shutdown":
                    log.info("Long press: stopping and shutting down")
                    stop_recording()
                    subprocess.run(["systemctl", "poweroff"], check=False)
                    break
                log.info("Long press ignored (long_press_action is 'none')")

            if recorder.recording:
                problem = recorder.check_alive()
                if problem:
                    log.error(problem)
                    if telem:
                        telem.end_session()
                        telem.status_text("Capture ERROR: pipeline died")
                    error = True

            now = time.monotonic()
            if recorder.recording and now - last_disk_check > cfg["storage"]["check_interval_s"]:
                last_disk_check = now
                if recorder.free_gb() < cfg["storage"]["min_free_gb"]:
                    log.error("Disk space low; stopping recording")
                    stop_recording()
                    error = True

            if led:
                led.state = "error" if error else ("recording" if recorder.recording else "idle")
    finally:
        stop_recording()
        for worker in (inp, led, telem):
            if worker:
                worker.close()
        log.info("Exited cleanly")


if __name__ == "__main__":
    main()
