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

# Allow-list of launcher commands STRINGS will run, keyed by the same
# `cmd` strings scrutinizer.py's APPS table uses. /assign takes this
# from network input, so validating against a known set (rather than
# building "/usr/local/bin/" + whatever-was-sent) is what stops a
# crafted app value like "../../../bin/sh" from becoming a path-
# traversal RCE -- subprocess.Popen([path]) doesn't go through a shell,
# but it will happily exec any file the resulting path resolves to.
KNOWN_APPS = {"bars", "loudness", "channel38", "weatherstar"}


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

    def run(self):
        while True:
            self.reload_event.clear()
            app = read_state().get("app")
            with self.lock:
                self.app = app

            if not app or app not in KNOWN_APPS:
                # Idle -- no valid assignment. Wait for one rather than
                # busy-looping; no console output is attempted here
                # (deferred: a real "waiting for assignment" screen).
                self.reload_event.wait()
                continue

            proc = subprocess.Popen([f"/usr/local/bin/{app}"])
            with self.lock:
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


def make_handler(supervisor):
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
                if app not in KNOWN_APPS:
                    self._send_json(400, {"error": f"unknown app {app!r}, must be one of {sorted(KNOWN_APPS)}"})
                    return
                supervisor.assign(app)
                self._send_json(200, {"ok": True, "app": app})
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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(supervisor))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    supervisor.run()


if __name__ == "__main__":
    main()
