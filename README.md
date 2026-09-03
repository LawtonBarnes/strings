# STRINGS (`strings.py`)

The puppet-side supervisor daemon for a [McBrain](https://github.com/LawtonBarnes/mcbrain)
fleet. Unlike its sibling apps ([BARS](https://github.com/LawtonBarnes/bars),
[LOUDNESS](https://github.com/LawtonBarnes/loudness), etc.), STRINGS
renders nothing itself -- it runs as a plain `systemd` service, not on
the physical console, since its only jobs are supervising whichever app
is currently assigned to this machine and reporting health back to
[CENTRAL SCRUTINIZER](https://github.com/LawtonBarnes/scrutinizer).

A machine's assigned app is exactly what starts on every power-up --
STRINGS-supervised machines are unattended by design, with no local
menu or interactivity of their own. All control happens remotely from
whichever machine runs SCRUTE.

## Install

```bash
sudo git clone https://github.com/LawtonBarnes/strings.git /opt/strings
sudo cp /opt/strings/strings.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strings
```

## HTTP API (port 8420, no auth -- trusted LAN only)

- `GET /status` -- current app, PID, app uptime, agent uptime/version,
  hostname, per-app hardware readiness (`"ready"`/`"not_installed"`/
  `"hardware_not_found"`), each installed app's own reported version,
  and system stats (CPU temp/clock/per-core, load average, memory,
  disk, WiFi signal/SSID).
- `POST /assign {"app": "<name>"}` -- switch this machine to a
  different app, or `null`/omit to clear back to idle. `<name>` must be
  one of the launcher commands in `KNOWN_APPS` -- validated against
  that allow-list rather than trusted as-is, since it becomes part of a
  filesystem path used to launch a subprocess. Returns `409` if the app
  is a real fleet-wide app but isn't actually installed on this
  specific machine.
- `POST /restart` -- restart the currently assigned app without
  changing it (used by `sync-fleet.sh` after a `git pull` picks up new
  code).
- `POST /input {"key": "<name>"}` -- relay one keypress to the running
  app's virtual input device (via `/dev/uinput`), for driving a remote
  machine's app live from SCRUTE's control mode. `<name>` must be one
  of a fixed allow-list (`RELAY_KEYS`) -- deliberately excludes
  Home/Back/Q/Esc/the hamburger key, which stay local to whichever
  machine is physically running SCRUTE (they mean "exit this app,"
  which only makes sense where a person is actually looking at the
  screen). Returns `503` if this machine's relay device wasn't
  available at startup.
- `POST /power {"action": "shutdown"|"restart"}` -- a real OS
  shutdown/reboot on this specific machine (used by SCRUTE's
  fleet-wide Power dialog, which calls this on every machine at once).

## Currently supported apps

`bars`, `loudness`, `channel38`, `weatherstar`, `bebop`, `joanjett` --
see `LAUNCH_COMMANDS`/`APP_SCRIPTS` near the top of `strings.py` for the
exact launcher/script paths each one maps to. Adding a new sibling app
means adding it to both of those dicts (and `VERSION_PATHS` if you want
its version to show up in `/status`) -- nothing else in this file
hardcodes the app roster.

## State

`state.json` (gitignored, not part of the repo) holds the current
assignment locally, so a machine keeps running its last-assigned app
across a reboot even if the SCRUTE machine is briefly unreachable.

## A note on orphaned processes

Every app's real launcher chain is `subprocess.Popen(["/usr/local/bin/<app>"])`
→ the launcher script itself, which `exec`s through `sudo openvt` when
STRINGS (a service with no console of its own) isn't already on tty1 --
so the PID STRINGS tracks directly is several process layers removed
from the actual running app. A plain `.terminate()`/`.kill()` on that
top PID doesn't reliably propagate down through `sudo`/`openvt`, so
STRINGS also runs an explicit `pkill -9 -f <script path>` sweep (for
every known app, not just the one being replaced) both after a normal
terminate/kill sequence and at the top of every launch cycle -- this
catches stray processes left behind by a crash or an earlier bug, not
just the immediately-preceding reassignment.
