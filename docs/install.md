# Install and update

**Tested on** a Raspberry Pi Zero 2 W running Raspberry Pi OS Lite 64-bit
(Trixie). The code uses the BCM hardware PWM on GPIO18/19, so a Pi 3, 4 or Zero 2
W should behave the same; a Pi 5 and 32-bit images are untested.

Flash Raspberry Pi OS Lite 64-bit with Raspberry Pi Imager, setting the hostname,
user, Wi-Fi, time zone and SSH there. Log in as that user (not root) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/BenLeikin/openSeedling/main/scripts/setup.sh | bash
```

That installs git, clones the app to `~/growlight` and runs the setup from the
checkout. It checks the board and OS first and stops on anything untested unless
you confirm or pass `--force`. Then it asks:

- which GPIO each device is on (Enter for the suggested pin, `none` for anything
  not wired);
- optional Anthropic API key and Discord webhook;
- on a first install, your latitude, longitude and time zone;
- whether to set a dashboard password, and whether to reboot.

Answers go to `.env` (pins and secrets, read by the systemd unit) and
`config.json` (location, password). Re-running offers your previous answers as
the defaults; `--defaults` skips every prompt. `--dir PATH` installs somewhere
other than `~/growlight`; `--repo URL` and `--branch NAME` install from a fork.

Already have a checkout? Run `bash scripts/setup.sh` from it.

Everything the script changes on the box comes from files in the repo:

| File | Applied to |
| --- | --- |
| `deploy/apt-packages.txt` | Debian packages |
| `deploy/boot-config.txt` | A managed block in `/boot/firmware/config.txt` |
| `deploy/growlight.service` | `/etc/systemd/system/growlight.service` |
| `deploy/env.example` | Reference for `.env` (not copied) |
| `requirements.txt` | Pinned venv packages |
| `requirements-optional.txt` | OpenCV and NumPy, allowed to fail |

The script is idempotent and never touches `config.json`, `growlight.db` or
`.secret`, so it is safe to re-run after an update. It handles:

1. Packages from `deploy/apt-packages.txt`
2. `.env`, keeping any key it does not ask about
3. Boot config: writes `deploy/boot-config.txt` as a `# BEGIN growlight` block at
   the end of `config.txt` and comments out conflicting lines elsewhere with a
   `#growlight# ` prefix, saving the old file alongside. **Any change here
   requires a reboot**, and the script says so.
4. `i2c-dev` in `/etc/modules`
5. Membership in the `gpio`, `i2c` and `video` groups
6. Swap: leaves an active swap (zram on Trixie) alone; creates a 1 GB swapfile
   only when there is none
7. A venv created with `--system-site-packages` (gpiozero and lgpio come from
   apt) and the pinned requirements
8. The systemd unit, enabled at boot; stray files in `growlight.service.d/` are
   reported, editor leftovers removed
9. A hardware check: which I2C addresses and DS18B20 sensors are actually visible

That last step is the useful one when something is not reading. It distinguishes
"the sensor is not wired" from "the software is not seeing it": if `i2cdetect`
does not list the address, no amount of restarting the service will help.

**Drift check.** `bash scripts/setup.sh --check` compares the box against
`deploy/` and the requirements and changes nothing; it exits 1 and shows a diff
when something has drifted. Any box-level change goes into `deploy/` or
`setup.sh` in the same commit as the code that needs it.

After the reboot:

1. Open `http://<pi>:5000`. If you skipped the location prompt, set Location in
   Settings first: the defaults are the author's (Thousand Oaks, CA), and sun
   times, the schedule and the DLI day all follow them.
2. Pick the light backend in Settings, Light, and run Calibrate on the light
   response card if the fixture dims.
3. Capture wet and dry anchors for each soil probe before turning on
   auto-watering.

## Run as a service

`scripts/setup.sh` writes and enables `/etc/systemd/system/growlight.service`
for you. Useful commands:

```bash
sudo systemctl status growlight
sudo systemctl restart growlight
journalctl -u growlight -f
```

## Deploy from a development checkout

When the Pi's `~/growlight` is a git checkout of the same remote, deploy from
your development machine with:

```bash
bash scripts/deploy.sh            # push main, pull on the Pi, restart, check
bash scripts/deploy.sh --check    # list what the Pi would get, change nothing
```

It pushes `main` to `origin`, then on the Pi (`--host`, default
`$GROWLIGHT_HOST` or `grow-server`) fast-forwards `~/growlight` to
`origin/main`, compiles every Python file, runs `scripts/setup.sh --defaults` if
`deploy/` or the requirements changed, restarts the service and waits for
`/api/status` with no tracebacks from the new process. If the code does not
compile or the service does not come back clean, the Pi goes back to the commit
it was on. It refuses to run with uncommitted changes, off `main`, or when the
Pi has commits that are not on `origin/main`.

## Update from a zip

A Pi that is not a git checkout can be updated from a zip of the changed files
(top folder `openSeedling/`, plus a `REMOVED` list for deleted files). Copy it
to the Pi and run:

```bash
bash ~/growlight/scripts/update.sh ~/openSeedling-update.zip
```

With no argument it uses the newest `openSeedling*.zip` in the current directory
or your home directory. It lists what changes and asks first. Then it backs up
`config.json`, `.env`, `.secret`, a consistent copy of `growlight.db` and every
file it replaces or deletes to `~/growlight-backups/` (newest five kept), copies
the new files in, compiles every Python file, runs `scripts/setup.sh --defaults`
if `deploy/` or the requirements changed, restarts the service and waits for
`/api/status` to answer with no tracebacks from the new process. If the code
does not compile or the service does not come back clean, it puts every file
back and restarts the old version. Box changes from `setup.sh` are not undone.
If `~/growlight` is a git checkout, the result is committed there.

`--check` lists what would change without touching anything; `--yes` skips the
question.

Static assets are versioned by file mtime, so the browser picks up new JS and
CSS on its own; no hard refresh needed.

## Reverse proxy

A separate box runs nginx and proxies the dashboard to the public site over HTTPS:

```nginx
location / {
    proxy_pass http://grow:5000;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
