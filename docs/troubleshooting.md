# Troubleshooting

- **Timelapse MP4 plays but is black.** A pixel-format/colour issue, not a corrupt
  file. The render forces limited-range `yuv420p`, a mod-16 height, and explicit
  BT.601 colour tags precisely so hardware decoders (VLC's especially) don't draw
  black. If an old file is black, just re-render. To check a file:
  `ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt,color_range,color_space ...`.
- **Discord returns HTTP 403.** Discord's Cloudflare blocks the default
  `Python-urllib` user agent; `discord_alert.py` sends a real `User-Agent`, which
  fixes it.
- **`lgpio` won't pip-install.** It comes from apt (`python3-lgpio`); the venv
  must be created with `--system-site-packages`. `setup.sh --check` flags a venv
  that was not.
- **A new AI report fires on every reboot.** The Pi Zero 2 W has no RTC, so at
  boot the clock is wrong until NTP corrects it. The report scheduler waits for
  the clock to sync before deciding anything, so it won't mistake the post-NTP
  time jump for the scheduled time. It reads the kernel's sync flag (the one
  behind `timedatectl`'s "System clock synchronized"), so it works with chrony as
  well as systemd-timesyncd.
- **Pump does nothing.** Check the separate 5 V supply and common ground, and that
  `gpiozero` loaded (the log prints if the GPIO backend is unavailable).
- **Photo looks zoomed in / cropped after a camera swap.** The capture resolution
  must match the sensor's native aspect ratio, or libcamera crops into the middle
  of the sensor. Set `cam_width`/`cam_height` to a 16:9 size for Module 3 (default
  2304x1296) or 2592x1944 for Module 1. After changing the frame, re-do the cell
  grid, since the old corners no longer line up.
- **"Capture failed" while the live preview works.** The preview runs at a lower
  resolution than a full capture. If Take photo fails but Align previews fine, the
  capture resolution is too large for available memory, which happens on the Pi
  Zero 2 W (512MB) at the sensor's full 4608x2592. Lower `cam_width`/`cam_height`
  to 2304x1296; the preview proves that size works on the hardware.
