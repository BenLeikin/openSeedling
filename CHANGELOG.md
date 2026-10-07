# Changelog

One entry per update zip, newest first. Install any update with
`bash scripts/update.sh openSeedling-update-N.zip`; each is cumulative from
the 4-17 baseline. The installer commits each one with the heading below.

## 41 (2026-10-06): Confirm before removing
- Removing a setup or a tray asks first. A tray with plants says how many planted cells it would discard, in one question.
- The Trays list's "filled" count ignores cleared cells.
- The smart plug's uses read "Light" and "Heat Mat".

## 40 (2026-10-05): Comments explain why
- History in code comments (names, dates, which update changed what) rewritten as the reason the code is that way; migrations are labeled as such.
- The heat mat's tuning story and current constants moved to `docs/heat-tuning.md`.
- A suite check keeps names and dates out of code comments.

## 39 (2026-10-05): Formatting and project hygiene
- Python formatted with `ruff format`, JavaScript and CSS with Prettier (`.prettierrc.json`); no behavior change.
- `pyproject.toml` with the version, this changelog, pre-commit hooks (`.pre-commit-config.yaml`).
- The installer's git commit names the version and this heading.
- The retired `static/app.js` is removed on install.
- Suite checks on the code's text ignore whitespace and optional semicolons, and find functions by brace matching.

## 38 (2026-10-05): Dashboard as ES modules
- `static/js/` modules with explicit imports and exports, entered through `main.js`; cross-module writes through `set_<name>()`; load-time code in each module's `start()`.
- Scripts served with `Cache-Control: no-cache`. ESLint configuration (`eslint.config.mjs`, `package.json`, development only).

## 37 (2026-10-04): Code structure
- `config.FORM` defines each Settings field once; validators, form attributes and the page's save and fill code come from it.
- Browser tests (`tests/test_ui.py`, Playwright). `routes.py` split into area modules; `app.js` split into ten scripts. `ruff.toml`.
- Fixed: the smart plug account name was erased by every Save.

## 36 (2026-10-04): Memory and storage
- All OpenCV work in `imgtool.py`, out of the controller. Memory charted under Device. Polled status built once per change; lite status for the touchscreen. Journal size capped. Oldest archived timelapse runs removed below 3 GB free.

## 35 (2026-10-03): Settings in eight sections
## 34 (2026-10-03): USB link speed guard and `scripts/usbcheck.sh`
## 33 (2026-10-03): One-page touchscreen summary (`/screen`)
## 32 (2026-10-03): Touchscreen kiosk (`scripts/kiosk.sh`, cage and cog under a memory cap)
## 31 (2026-09-29): Phone layout (tray scrolls in its card); planting map count
## 30 (2026-09-29): Fixed: Save did nothing after update 29 (form validation moved to the server)
## 29 (2026-09-28): Separate player and video speeds; a new video speed re-renders
## 28 (2026-09-27): Code review fixes
- Atomic settings writes with a backup, no fallback to passwordless defaults; pump time caps on the monotonic clock; disk-space guard and alert; capture loop survives errors; last complete frame; reduced-scale decoding.

## 27 (2026-09-27): Balanced full-width chart rows
## 26 (2026-09-27): Charts drawn at their real size (no stretched text)
## 25 (2026-09-27): Chart axes, lights-off shading, one crosshair across all charts, 6 h and 3 d ranges
## 24 (2026-09-26): Timelapse speed setting
## 23 (2026-09-26): The main Save also saves Setups edits
## 22 (2026-09-26): Heat mat controller retuned from the rig's measured response; power level logged
## 21 (2026-09-26): Heat mat Auto as time-proportional PI control
## 20 (2026-09-25): Shared sensors across setups; heat mat as a setup item
## 19 (2026-09-25): Light card device rows
## 18 (2026-09-25): Readable settings error messages
## 17 (2026-09-25): Heat mat thermostat on the smart plug (`heat.py`)
## 16 (2026-09-24): Canopy readings not stale at night
## 15 (2026-09-24): Photos leave the light alone by default
## 14 (2026-09-24): Out-of-memory fixes (CMA 64 MB, two capture buffers, OOM-first helpers)
## 11 (2026-09-23): Unsaved settings no longer reverted by incoming statuses
## 10 (2026-09-23): Startup log noise; thumbnail race
## 9 (2026-09-23): AI report grouped by setup
## 8 (2026-09-22): Enlarged view opens on a single click
## 7 (2026-09-22): Enlarged view of photos and timelapse frames
## 6 (2026-09-22): Sharper paused timelapse frames; render quality
## 5 (2026-09-21): `growlight.py` split into modules
