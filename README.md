# STRINGS (`strings.py`)

The puppet-side supervisor daemon for the McBrain fleet (`puppet1`-`4`).
Unlike its sibling apps ([BARS](https://github.com/LawtonBarnes/bars),
[LOUDNESS](https://github.com/LawtonBarnes/loudness),
[CHANNEL 38](https://github.com/LawtonBarnes/channel38)), STRINGS renders
nothing itself -- it runs as a plain `systemd` service, not on the
physical console, since its only jobs are supervising whichever app is
currently assigned to this puppet and reporting health back to
[CENTRAL SCRUTINIZER](https://github.com/LawtonBarnes/scrutinizer) on
`masterofpuppets` (MP).

A puppet's assigned app is exactly what starts on every power-up --
puppets are unattended by design, with no local menu or interactivity.
All control happens from MP.

## Install

```bash
sudo git clone https://github.com/LawtonBarnes/strings.git /opt/strings
sudo cp /opt/strings/strings.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strings
```

## HTTP API (port 8420, no auth -- trusted LAN only)

- `GET /status` -- current app, PID, app uptime, agent uptime/version, and
  system stats (CPU temp/clock/per-core, load average, memory, disk).
- `POST /assign {"app": "<name>"}` -- switch this puppet to a different
  app. `<name>` must be one of the launcher commands in `KNOWN_APPS`
  (`bars`/`loudness`/`channel38`/`weatherstar`) -- validated against that
  allow-list rather than trusted as-is, since it becomes part of a
  filesystem path used to launch a subprocess.
- `POST /restart` -- restart the currently assigned app without changing
  it (used by `sync-fleet.sh` after a `git pull` picks up a new version).

## State

`state.json` (gitignored, not part of the repo) holds the current
assignment locally, so a puppet keeps running its last-assigned app
across a reboot even if MP is briefly unreachable.
