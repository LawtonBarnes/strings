#!/usr/bin/env python3
"""STRINGS -- puppet-side supervisor daemon for the McBrain fleet.

Runs as a systemd service on puppet1-4 (never on masterofpuppets/MP or
production/PR -- see the McBrain fleet-management plan). Unlike its
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

import psutil

VERSION = "1.0"

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state.json"
PORT = 8420
RESTART_BACKOFF_SECONDS = 3
TERM_GRACE_SECONDS = 3
POLL_INTERVAL_SECONDS = 1
HARDWARE_REFRESH_SECONDS = 30  # how often to re-poll per-app readiness (same cadence as scrutinizer.py's)

# Allow-list of launcher commands STRINGS will run, keyed by the same
# `cmd` strings scrutinizer.py's APPS table uses, plus the special
# "identify" pseudo-app below. /assign takes this from network input,
# so validating against a known set (rather than building
# "/usr/local/bin/" + whatever-was-sent) is what stops a crafted app
# value like "../../../bin/sh" from becoming a path-traversal RCE --
# subprocess.Popen([path]) doesn't go through a shell, but it will
# happily exec any file the resulting path resolves to.
#
# "identify" isn't a real sibling app -- it's bars.py with its hostname
# overlay forced on via --identify (no keypress needed, since puppets
# have no input device), used to figure out which physical Pi/CRT is
# which after the McBrain stack gets moved and recabled. It's also
# IDLE_APP below: what runs whenever no real assignment exists, instead
# of a puppet just sitting there doing nothing.
LAUNCH_COMMANDS = {
    "bars": ["/usr/local/bin/bars"],
    "loudness": ["/usr/local/bin/loudness"],
    "channel38": ["/usr/local/bin/channel38"],
    "weatherstar": ["/usr/local/bin/weatherstar"],
    "identify": ["/usr/local/bin/bars", "--identify"],
}
KNOWN_APPS = set(LAUNCH_COMMANDS)
IDLE_APP = "identify"


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


def gather_stats():
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_temp": get_cpu_temp(),
        "cpu_clock_mhz": get_cpu_clock_mhz(),
        "cpu_percore": psutil.cpu_percent(percpu=True),
        "loadavg": list(os.getloadavg()),
        "mem": {"percent": mem.percent, "used": mem.used, "total": mem.total},
        "disk": {"percent": disk.percent, "used": disk.used, "free": disk.free},
    }


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


# None = always ready once the launcher exists (matches scrutinizer.py's
# APPS table: BARS has no hw_check either). "identify" is deliberately
# excluded -- it's not a real assignable app, it's always available via
# bars.py itself.
HW_CHECKS = {
    "bars": None,
    "loudness": check_loudness_mic,
    "weatherstar": check_internet,
    "channel38": check_internet,
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
        readiness = {}
        for app, check in HW_CHECKS.items():
            if not Path(f"/usr/local/bin/{app}").exists():
                readiness[app] = False  # not even installed on this puppet
                continue
            if check is None:
                readiness[app] = True
                continue
            try:
                readiness[app] = check()
            except Exception:
                readiness[app] = False
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
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

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
                # No real assignment yet -- default to IDLE_APP (SMPTE
                # bars + hostname overlay) rather than a blank screen,
                # so an unassigned puppet is still useful for figuring
                # out which physical box it is. Doesn't touch
                # state.json -- the *real* assignment (or lack of one)
                # stays whatever it was, this is just what runs.
                app = IDLE_APP

            proc = self._try_launch(app)
            if proc is None and app != IDLE_APP:
                # /assign already rejects apps whose launcher isn't
                # installed here (see do_POST), but this is the backstop
                # for anything that slips through anyway -- a race, a
                # hand-edited state.json, a launcher removed after being
                # assigned. Don't retry the same broken assignment in a
                # tight loop forever; fall back to the idle default
                # instead. state.json itself is untouched, so fixing the
                # underlying problem (installing the app, reassigning)
                # is picked up automatically on the next cycle.
                print(f"[strings] falling back to {IDLE_APP!r}", flush=True)
                app = IDLE_APP
                proc = self._try_launch(app)
            if proc is None:
                # Even the idle fallback failed (e.g. bars itself is
                # missing) -- nothing left to try this cycle.
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
                print(f"[strings] {app} exited (code {proc.returncode}), "
                      f"restarting in {RESTART_BACKOFF_SECONDS}s", flush=True)
                time.sleep(RESTART_BACKOFF_SECONDS)


def make_handler(supervisor, readiness_checker):
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
                # None/missing/"" explicitly clears the assignment
                # (falls back to IDLE_APP) rather than being rejected --
                # distinct from an actually-unrecognized app name.
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
                # silently-broken assignment.
                if app and not Path(f"/usr/local/bin/{app}").exists():
                    self._send_json(409, {"error": f"{app!r} is a known app but isn't installed on this puppet"})
                    return
                supervisor.assign(app or None)
                self._send_json(200, {"ok": True, "app": app or None})
            elif self.path == "/restart":
                supervisor.restart()
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            pass  # keep routine requests out of the journal

    return Handler


def main():
    supervisor = Supervisor()
    readiness_checker = ReadinessChecker()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(supervisor, readiness_checker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    supervisor.run()


if __name__ == "__main__":
    main()
