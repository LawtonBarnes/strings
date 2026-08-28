#!/usr/bin/env python3
"""STRINGS -- puppet-side supervisor daemon for the McBrain fleet.

Runs as a systemd service on puppet1-4 and production/PR (joined as a
real STRINGS-supervised target 2026-08-16 -- see the McBrain fleet-
management plan), never on masterofpuppets/MP, which stays the
control/monitoring hub. Unlike its
sibling apps (bars/loudness/channel38/weatherstar), STRINGS renders
nothing itself -- it has no display code at all, so it doesn't need
console/tty1 ownership and runs as a plain systemd service (Restart=
always) instead of the tty1/.bashrc pattern those apps use. Its only two
jobs: supervise whichever app is currently assigned (launch, monitor,
auto-restart on crash or on command), and expose a small HTTP/JSON API
so SCRUTE on MP can poll this puppet's health and remotely change its
assignment. A puppet's assigned app is what starts on every power-up,
persisted locally in state.json -- STRINGS itself never renders a menu
or dashboard, puppets are unattended by design.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import evdev
import psutil
from evdev import ecodes

VERSION = "1.8"

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state.json"
PORT = 8420
RESTART_BACKOFF_SECONDS = 3
# openvt's own exit code (propagated through sudo) when it fails to grab
# VT1 right after a switch, printing "Couldn't deallocate console 1" --
# a narrow, self-identifying kernel-level VT-subsystem race between one
# app's teardown and the next app's openvt invocation (see
# _kill_stray_apps() -- mitigated there, not fully eliminated even with
# getty@tty1.service disabled on every puppet, confirmed live 2026-08-16).
# Since this exact code means "openvt lost a timing race, not a real
# app crash," retrying near-instantly here is safe and turns what would
# otherwise be a full RESTART_BACKOFF_SECONDS-long visible blip into a
# sub-second one -- any *other* exit code still gets the normal backoff,
# so a genuinely broken app still can't tight-loop.
OPENVT_RACE_EXIT_CODE = 8
OPENVT_RACE_BACKOFF_SECONDS = 0.2
TERM_GRACE_SECONDS = 3
POLL_INTERVAL_SECONDS = 1
HARDWARE_REFRESH_SECONDS = 30  # how often to re-poll per-app readiness (same cadence as scrutinizer.py's)
WIFI_IFACE = "wlan0"

# Keys SCRUTE can relay live to whatever app is currently running here
# (see /input below) -- content-adjustment keys (BARS pattern cycling,
# LOUDNESS gain, Vol mute), NOT Home/Back/Q/Esc/Compose. Those already
# mean "exit this app" in every app's own handle_keycode (EXIT_GOTO_HOME,
# sys.exit, etc.) -- relaying them would kill/restart whatever's running
# instead of just adjusting it. SCRUTE itself is what those keys act on
# locally when a puppet is selected. Power used to be relayed here too
# (2026-08-17, so the *target*'s own confirm dialog showed on the
# machine being controlled) -- removed 2026-08-27 when Power became a
# fleet-wide action instead of a single-machine one; see /power below,
# which now handles it independent of whatever app is running.
RELAY_KEYS = {
    "KEY_UP": ecodes.KEY_UP,
    "KEY_DOWN": ecodes.KEY_DOWN,
    "KEY_LEFT": ecodes.KEY_LEFT,
    "KEY_RIGHT": ecodes.KEY_RIGHT,
    "KEY_ENTER": ecodes.KEY_ENTER,
    "KEY_VOLUMEUP": ecodes.KEY_VOLUMEUP,
    "KEY_VOLUMEDOWN": ecodes.KEY_VOLUMEDOWN,
    # bebop is the first app where Back means "go up a menu level"
    # rather than "exit the app" -- see scrutinizer.py's
    # _handle_control_mode_keycode for the app-aware relay decision
    # this key needs on the SCRUTE side.
    "KEY_BACK": ecodes.KEY_BACK,
}

# Actions for /power (2026-08-27) -- a real OS shutdown/reboot on this
# machine, independent of whatever app is currently assigned/running
# here. Distinct from the old Power-key /input relay above (which just
# forwarded a keypress to the running app's own confirm dialog,
# single-target only) -- this is SCRUTE's new fleet-wide Power button,
# which now always means "take down every machine at once" rather than
# whichever one currently owns the remote/control-mode target. logind's
# own HandlePowerKey=ignore (set fleet-wide 2026-08-17) is unrelated to
# this -- that only stops a raw physical KEY_POWER evdev event from
# instantly powering things off; this endpoint runs a normal `shutdown`
# command directly, same as every sibling app's own local power dialog
# already does.
POWER_ACTIONS = {
    "shutdown": ["sudo", "shutdown", "-h", "now"],
    "restart": ["sudo", "shutdown", "-r", "now"],
}

# Allow-list of launcher commands STRINGS will run, keyed by the same
# `cmd` strings scrutinizer.py's APPS table uses. /assign takes this
# from network input, so validating against a known set (rather than
# building "/usr/local/bin/" + whatever-was-sent) is what stops a
# crafted app value like "../../../bin/sh" from becoming a
# path-traversal RCE -- subprocess.Popen([path]) doesn't go through a
# shell, but it will happily exec any file the resulting path resolves
# to.
LAUNCH_COMMANDS = {
    "bars": ["/usr/local/bin/bars"],
    "loudness": ["/usr/local/bin/loudness"],
    "channel38": ["/usr/local/bin/channel38"],
    "weatherstar": ["/usr/local/bin/weatherstar"],
    "bebop": ["/usr/local/bin/bebop"],
    "joanjett": ["/usr/local/bin/joanjett"],
}
KNOWN_APPS = set(LAUNCH_COMMANDS)

# Absolute script paths for the actual pkill sweep below -- distinct
# from LAUNCH_COMMANDS, whose values are the /usr/local/bin/<app>
# *launcher* (which wraps the real script in `sudo openvt ...` when not
# already on tty1 -- see each launcher's own comment).
APP_SCRIPTS = {
    "bars": "/opt/bars/bars.py",
    "loudness": "/opt/loudness/loudness.py",
    "channel38": "/opt/channel38/channel38.py",
    "weatherstar": "/opt/weatherstar/weatherstar_launcher.py",
    "bebop": "/opt/bebop/bebop.py",
    "joanjett": "/opt/joanjett/main.py",
}

# Paths STRINGS scans for each app's own VERSION constant, exposed via
# /status's `versions` field (2026-08-23) so SCRUTE can show a puppet's
# real installed version on the "ASSIGN TO <puppet>" menu instead of
# MP's own (often absent) local copy of that app. Distinct from
# APP_SCRIPTS above (used for the pkill sweep) since bebop.py itself
# re-exports VERSION from menu.py rather than defining it directly
# (`VERSION = menu.VERSION`, not a quoted literal) -- VERSION_RE below
# only matches the latter, so bebop needs its own entry pointing at the
# file that actually has one.
VERSION_PATHS = {**APP_SCRIPTS, "bebop": "/opt/bebop/menu.py", "joanjett": "/opt/joanjett/config.py"}

VERSION_RE = re.compile(r"""VERSION\s*=\s*['"]([^'"]+)['"]""")


def read_app_version(script_path):
    # Mirrors scrutinizer.py's own read_app_version exactly (same
    # no-shared-library convention as the other duplicated helpers in
    # this file) -- scans the source text for the VERSION constant
    # rather than importing the module, so a puppet doesn't need that
    # app's own dependencies/venv just to answer "what version is this."
    try:
        text = Path(script_path).read_text()
    except OSError:
        return "?"
    match = VERSION_RE.search(text)
    return match.group(1) if match else "?"


def read_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state):
    STATE_PATH.write_text(json.dumps(state))


# Stat-gathering helpers deliberately duplicated from scrutinizer.py
# rather than imported from a shared module -- matches this project's
# established no-shared-library convention (see the BARS/LOUDNESS/
# CHANNEL 38 memory notes: each app owns its own copy).
def get_cpu_temp():
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=3
        )
        return float(result.stdout.strip().removeprefix("temp=").removesuffix("'C"))
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def get_cpu_clock_mhz():
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_clock", "arm"], capture_output=True, text=True, timeout=3
        )
        return int(result.stdout.strip().split("=")[1]) / 1_000_000
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return None


def get_throttled_status():
    # Duplicated from scrutinizer.py's get_throttled_status() -- see its
    # own comment for the bit-field rationale (current-condition bits
    # 0-3 only; the "has happened since boot" bits 16-19 aren't
    # actionable on a live dashboard).
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3
        )
        value = int(result.stdout.strip().split("=")[1], 16)
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return "UNKNOWN"
    if value & 0x1:
        return "UNDERVOLTAGE"
    if value & 0x4:
        return "THROTTLED"
    if value & 0x8:
        return "TEMP LIMIT"
    if value & 0x2:
        return "FREQ CAPPED"
    return "OK"


def get_ip_address():
    # Same UDP-connect trick as scrutinizer.py's/bars.py's get_ip_address()
    # -- doesn't actually send anything, just asks the kernel which local
    # address would be used to reach an external host.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "NO NETWORK"
    finally:
        s.close()


def get_wifi_link():
    """Returns (quality_0_to_70, level_dbm) for WIFI_IFACE from
    /proc/net/wireless, or (None, None) if not found -- duplicated
    verbatim from scrutinizer.py's get_wifi_link()."""
    try:
        lines = Path("/proc/net/wireless").read_text().splitlines()
    except OSError:
        return None, None
    for line in lines:
        line = line.strip()
        if not line.startswith(f"{WIFI_IFACE}:"):
            continue
        fields = line.split(":", 1)[1].split()
        try:
            quality = float(fields[1])
            level = float(fields[2])
            return quality, level
        except (IndexError, ValueError):
            return None, None
    return None, None


def get_wifi_ssid():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    return None


def gather_stats():
    # wifi_ssid/wifi_quality/wifi_level/ip added 2026-08-15 -- without
    # these, SCRUTE's WIFI panel for every puppet fell back to its
    # "NOT CONNECTED"/"N/A" placeholders regardless of actual link
    # state, which reads as a real outage even when the puppet is
    # plainly reachable (its own /status request just answered).
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    wifi_quality, wifi_level = get_wifi_link()
    return {
        "cpu_temp": get_cpu_temp(),
        "cpu_clock_mhz": get_cpu_clock_mhz(),
        "throttled": get_throttled_status(),
        "cpu_percore": psutil.cpu_percent(percpu=True),
        "loadavg": list(os.getloadavg()),
        "mem": {"percent": mem.percent, "used": mem.used, "total": mem.total},
        "disk": {"percent": disk.percent, "used": disk.used, "free": disk.free},
        "wifi_ssid": get_wifi_ssid(),
        "wifi_quality": wifi_quality,
        "wifi_level": wifi_level,
        "ip": get_ip_address(),
    }


def create_relay_device():
    """A persistent, never-removed virtual keyboard for /input to emit
    synthetic keypresses through -- created once here, before
    Supervisor.run() ever launches an app, so it already exists by the
    time that app's own find_keyboard_devices()-style evdev scan runs at
    ITS startup (every sibling app already enumerates all EV_KEY-capable
    devices rather than hardcoding paths, so this needs zero app-side
    changes to be picked up). Needs metalshop to have write access to
    /dev/uinput -- already granted fleet-wide via the udev rule added
    for WEATHERSTAR's dummy-pointer fix (/etc/udev/rules.d/99-uinput.rules).
    Returns None if creation fails (e.g. rule missing on a not-yet-
    updated puppet) so /input can degrade to reporting an error instead
    of crashing the whole daemon over it."""
    try:
        return evdev.UInput(
            {ecodes.EV_KEY: list(RELAY_KEYS.values())},
            name="strings-remote-relay",
        )
    except OSError as exc:
        print(f"[strings] couldn't create relay input device: {exc}", flush=True)
        return None


def send_relay_key(device, key_name):
    """Emits one keydown+keyup (with SYN_REPORT after each) for key_name
    through device -- matches a real keypress exactly, so every app's
    existing evdev-reading code (already watching event.value == 1 for
    "pressed") handles it with no special-casing."""
    code = RELAY_KEYS[key_name]
    device.write(ecodes.EV_KEY, code, 1)
    device.syn()
    device.write(ecodes.EV_KEY, code, 0)
    device.syn()


# Per-app readiness checks -- answers "could THIS puppet actually run
# that app right now," which is what an assignment decision from MP
# needs (unlike GET /status's cpu/mem/disk, which is about this
# puppet's health generally). Duplicated from scrutinizer.py's
# check_loudness_mic/check_internet rather than imported, same
# no-shared-library convention as the stat-gathering helpers above.
LOUDNESS_SETTINGS_PATH = "/opt/loudness/settings.ini"
LOUDNESS_DEVICE_RE = re.compile(r"^device\s*=\s*plughw:(\d+),(\d+)", re.MULTILINE)


def check_loudness_mic():
    try:
        text = Path(LOUDNESS_SETTINGS_PATH).read_text()
    except OSError:
        return False
    match = LOUDNESS_DEVICE_RE.search(text)
    if not match:
        return False
    card, device = match.groups()
    return Path(f"/dev/snd/pcmC{card}D{device}c").exists()


def check_internet():
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=1.5):
            return True
    except OSError:
        return False


def check_mpd():
    try:
        with socket.create_connection(("localhost", 6600), timeout=1.5):
            return True
    except OSError:
        return False


# None = always ready once the launcher exists (matches scrutinizer.py's
# APPS table: BARS has no hw_check either).
HW_CHECKS = {
    "bars": None,
    "loudness": check_loudness_mic,
    "weatherstar": check_internet,
    "channel38": check_internet,
    "bebop": check_mpd,
    "joanjett": None,
}


class ReadinessChecker:
    """Background-thread polling of per-app readiness, same shape as
    RemotePoller on the SCRUTE side -- these checks (network calls,
    file/device reads) are cheap individually but no reason to redo them
    on every GET /status, so they're computed on a timer and cached."""

    def __init__(self):
        self._lock = threading.Lock()
        self._readiness = {}
        threading.Thread(target=self._run, daemon=True).start()

    def get(self):
        with self._lock:
            return dict(self._readiness)

    def _run(self):
        while True:
            result = self._compute()
            with self._lock:
                self._readiness = result
            time.sleep(HARDWARE_REFRESH_SECONDS)

    def _compute(self):
        # Status strings instead of plain bools (2026-08-15) -- "not
        # installed on this puppet at all" and "installed but its own
        # hw_check failed" used to collapse into the same `False`, which
        # SCRUTE then always displayed as "HARDWARE NOT FOUND" -- deeply
        # confusing for WEATHERSTAR/CHANNEL 38 specifically, since
        # neither has ever been installed on any puppet and neither
        # check is actually about physical hardware (check_internet).
        # Caught live: user saw "HARDWARE NOT FOUND" under two apps that
        # "don't require a device."
        readiness = {}
        for app, check in HW_CHECKS.items():
            if not Path(f"/usr/local/bin/{app}").exists():
                readiness[app] = "not_installed"
                continue
            if check is None:
                readiness[app] = "ready"
                continue
            try:
                readiness[app] = "ready" if check() else "hardware_not_found"
            except Exception:
                readiness[app] = "hardware_not_found"
        return readiness


class Supervisor:
    """Owns the currently-running app subprocess. One assigned app at a
    time -- assign()/restart() just set an Event; the run() loop (main
    thread) is the only thing that actually starts/stops processes, so
    there's no race between an HTTP request thread and the supervisor
    touching the same Popen object."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.app = None
        self.app_started_at = None
        self.reload_event = threading.Event()
        self.start_time = time.time()

    def current_status(self):
        with self.lock:
            alive = self.proc is not None and self.proc.poll() is None
            return {
                "app": self.app,
                "pid": self.proc.pid if alive else None,
                "app_uptime_s": (
                    round(time.time() - self.app_started_at, 1)
                    if (alive and self.app_started_at)
                    else None
                ),
            }

    def assign(self, app):
        write_state({"app": app})
        self.reload_event.set()

    def restart(self):
        self.reload_event.set()

    def _terminate_current(self):
        with self.lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._kill_stray_apps()

    @staticmethod
    def _kill_stray_apps():
        # Belt-and-suspenders cleanup, confirmed necessary live: proc
        # above is the launcher's own Popen handle, which is `sudo
        # openvt -f -c 1 -s -w -- python3 <script>` whenever STRINGS
        # isn't itself on tty1 (systemd services never are -- see each
        # /usr/local/bin/<app> launcher's own comment). SIGTERM/SIGKILL
        # sent to that top PID kills `sudo`, but sudo/openvt don't
        # reliably forward it down to the real python3 process --
        # confirmed live after several reassignment cycles left 2-3
        # generations of orphaned bars.py/loudness.py processes (PPID 1)
        # all still running and fighting over /dev/fb0 and tty1. Killing
        # by the known script path is robust regardless of how many
        # sudo/openvt layers deep the real process ended up, and is safe
        # to run unconditionally since only STRINGS ever launches these
        # on a puppet -- there's never a legitimate reason for one of
        # them to be running outside of what STRINGS itself just started.
        #
        # Kill only the deepest python3 process first, THEN sweep
        # everything as a backstop (2026-08-16) -- the previous version
        # pattern-killed sudo/openvt/python3 all in the same instant,
        # which denied openvt (running with -w, i.e. "wait for my
        # child") any chance to take its own natural, clean exit path:
        # confirmed live via strace that killing the whole chain at once
        # reliably made the *next* app's openvt invocation fail once
        # ("openvt: Couldn't deallocate console 1", exit code 8, sudo
        # propagating openvt's own exit status) before self-healing via
        # STRINGS's normal 3s crash-restart -- visible as a
        # launch-quit-relaunch blip on every app switch. Never
        # reproduced from a cold launch with nothing to clean up
        # (nothing alive to interrupt), only ever on a genuine switch --
        # consistent with this exact race. Killing just python3 first
        # lets openvt see its own child exit and unwind through its own
        # designed cleanup instead of being cut off mid-flight; the
        # anchored ^python3 pattern (vs a bare substring match) is what
        # keeps this from also matching the sudo/openvt layers, whose
        # command lines contain the same script path as a later
        # argument. The broad sweep below is unchanged from before and
        # still SIGKILLs by pattern across all 3 layers -- this is the
        # original orphan-pileup backstop (bars.py/loudness.py found
        # alive 2-3 generations deep after SIGTERM alone didn't
        # propagate through sudo/openvt), preserved as a fallback for
        # anything that doesn't exit on its own in the grace window.
        #
        # Bug found 2026-08-28: the anchor assumed every app runs under
        # the bare system `python3` -- true for bars/loudness/channel38/
        # weatherstar, but bebop uses its own venv interpreter
        # (/opt/bebop/venv/bin/python3, see /usr/local/bin/bebop), whose
        # command line never starts with the literal string "python3".
        # The old `^python3 ` anchor silently never matched bebop's real
        # process at all -- it always skipped straight to the -9 sweep
        # below, meaning bebop never got a graceful SIGTERM/finally-
        # block chance to clean up (e.g. pausing MPD) on restart/
        # reassign, only a hard SIGKILL. `[^ ]*` allows any no-space
        # interpreter path (venv or bare) in front of "python3 " while
        # still excluding the sudo/openvt wrapper line, whose command
        # starts with "sudo " -- a space right after "sudo" breaks the
        # match at that position, and the `^` anchor prevents retrying
        # elsewhere in the string, so it's still correctly excluded.
        KILL_GRACE_SECONDS = 1.0
        for script in APP_SCRIPTS.values():
            subprocess.run(["sudo", "pkill", "-15", "-f", f"^[^ ]*python3 {script}"], capture_output=True)
        time.sleep(KILL_GRACE_SECONDS)
        for script in APP_SCRIPTS.values():
            subprocess.run(["sudo", "pkill", "-9", "-f", script], capture_output=True)

    def _try_launch(self, app):
        try:
            return subprocess.Popen(LAUNCH_COMMANDS[app])
        except OSError as exc:
            print(f"[strings] failed to launch {app!r}: {exc}", flush=True)
            return None

    def run(self):
        while True:
            self.reload_event.clear()
            app = read_state().get("app")
            if not app or app not in KNOWN_APPS:
                # No real assignment -- sit genuinely idle (nothing
                # running) until an /assign call sets one and wakes
                # reload_event. Doesn't touch state.json -- the *real*
                # assignment (or lack of one) stays whatever it was.
                self._kill_stray_apps()
                with self.lock:
                    self.app = None
                    self.proc = None
                    self.app_started_at = None
                self.reload_event.wait(POLL_INTERVAL_SECONDS)
                continue

            self._kill_stray_apps()
            proc = self._try_launch(app)
            if proc is None:
                # /assign already rejects apps whose launcher isn't
                # installed here (see do_POST), but this is the backstop
                # for anything that slips through anyway -- a race, a
                # hand-edited state.json, a launcher removed after being
                # assigned. Don't retry the same broken assignment in a
                # tight loop forever; state.json itself is untouched, so
                # fixing the underlying problem (installing the app,
                # reassigning) is picked up automatically next cycle.
                print(f"[strings] failed to launch {app!r}, idling", flush=True)
                time.sleep(RESTART_BACKOFF_SECONDS)
                continue

            with self.lock:
                self.app = app
                self.proc = proc
                self.app_started_at = time.time()

            while proc.poll() is None and not self.reload_event.is_set():
                time.sleep(POLL_INTERVAL_SECONDS)

            if self.reload_event.is_set():
                self._terminate_current()
            else:
                backoff = (
                    OPENVT_RACE_BACKOFF_SECONDS
                    if proc.returncode == OPENVT_RACE_EXIT_CODE
                    else RESTART_BACKOFF_SECONDS
                )
                print(f"[strings] {app} exited (code {proc.returncode}), "
                      f"restarting in {backoff}s", flush=True)
                time.sleep(backoff)


def make_handler(supervisor, readiness_checker, relay_device):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/status":
                status = supervisor.current_status()
                status.update(gather_stats())
                status["agent_version"] = VERSION
                status["agent_uptime_s"] = round(time.time() - supervisor.start_time, 1)
                status["hardware"] = readiness_checker.get()
                status["hostname"] = socket.gethostname()
                status["versions"] = {app: read_app_version(path) for app, path in VERSION_PATHS.items()}
                self._send_json(200, status)
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}

            if self.path == "/assign":
                app = payload.get("app")
                # None/missing/"" explicitly clears the assignment (sits
                # idle -- see Supervisor.run()) rather than being
                # rejected -- distinct from an actually-unrecognized app
                # name.
                if app not in KNOWN_APPS and app is not None and app != "":
                    self._send_json(400, {"error": f"unknown app {app!r}, must be one of {sorted(KNOWN_APPS)} or null to clear"})
                    return
                # A name can be a *known* app fleet-wide (it's in
                # KNOWN_APPS) without being installed on *this* puppet --
                # reject that here rather than accepting an assignment
                # that can only ever fail to launch. The Supervisor loop
                # also guards against this independently (see
                # _try_launch), but rejecting up front means the caller
                # (SCRUTE) gets a clear, immediate answer instead of a
                # silently-broken assignment. Checks the app's actual
                # launcher binary (LAUNCH_COMMANDS[app][0]), not a path
                # built from the app name.
                if app and not Path(LAUNCH_COMMANDS[app][0]).exists():
                    self._send_json(409, {"error": f"{app!r} is a known app but isn't installed on this puppet"})
                    return
                supervisor.assign(app or None)
                self._send_json(200, {"ok": True, "app": app or None})
            elif self.path == "/restart":
                supervisor.restart()
                self._send_json(200, {"ok": True})
            elif self.path == "/input":
                key = payload.get("key")
                if key not in RELAY_KEYS:
                    self._send_json(400, {"error": f"unknown key {key!r}, must be one of {sorted(RELAY_KEYS)}"})
                    return
                if relay_device is None:
                    # Only happens if /dev/uinput wasn't writable at
                    # startup (see create_relay_device) -- a 503 tells
                    # SCRUTE this puppet genuinely can't relay right now,
                    # distinct from a bad request.
                    self._send_json(503, {"error": "relay device unavailable on this puppet"})
                    return
                send_relay_key(relay_device, key)
                self._send_json(200, {"ok": True})
            elif self.path == "/power":
                action = payload.get("action")
                if action not in POWER_ACTIONS:
                    self._send_json(400, {"error": f"unknown action {action!r}, must be one of {sorted(POWER_ACTIONS)}"})
                    return
                # Respond before actually running shutdown -- once it
                # goes through, this process (and everything else on the
                # machine) gets torn down shortly after, so the caller
                # needs its 200 OK sent first, not queued behind a
                # subprocess call that's about to kill the process that
                # would send it.
                self._send_json(200, {"ok": True})
                subprocess.run(POWER_ACTIONS[action])
            else:
                self._send_json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            pass  # keep routine requests out of the journal

    return Handler


def main():
    supervisor = Supervisor()
    readiness_checker = ReadinessChecker()
    # Created before supervisor.run() ever launches an app -- see
    # create_relay_device()'s docstring for why the ordering matters.
    relay_device = create_relay_device()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(supervisor, readiness_checker, relay_device))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    supervisor.run()


if __name__ == "__main__":
    main()
