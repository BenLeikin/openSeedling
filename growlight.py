#!/usr/bin/env python3
"""
Grow light controller + dashboard + timelapse for Raspberry Pi.

Drives a logic-level MOSFET on GPIO18 via the kernel's hardware PWM,
following local sunrise and sunset with smooth fade-in / fade-out ramps.
Serves a dashboard on http://<pi-ip>:5000 with live-editable settings.
Optionally captures timelapse photos at a fixed interval during the
photoperiod, holding the light at a fixed brightness for each shot so
every frame is identically exposed. Photos land in ./timelapse/.

Requires (handled by setup.sh):
  - 'dtoverlay=pwm,pin=18,func=2' in /boot/firmware/config.txt, then reboot
  - rpicam-apps (apt) for the camera
  - pip install astral rpi-hardware-pwm flask

This file is the entry point. The code lives in config, hardware, light,
setups, water, monitor, camera, status and routes; see the README.
"""

import signal
import threading

from applog import log

# The app is split into modules, each importing the ones below it. Importing
# them here, in this order, is what starts the app: config loads settings,
# hardware claims the pins, light starts the dither thread, status arms the
# switch watchers, routes builds the Flask app.
import config
import hardware
import light as light_mod
import setups as setups_mod    # noqa: F401
import water
import monitor
import camera as camera_mod
import status as status_mod    # noqa: F401
import routes

signal.signal(signal.SIGINT, hardware.cleanup)
signal.signal(signal.SIGTERM, hardware.cleanup)

if __name__ == "__main__":
    water.restore_persistent_state()      # before anything can water
    threading.Thread(target=light_mod.control_loop, daemon=True).start()
    threading.Thread(target=camera_mod.capture_loop, daemon=True).start()
    threading.Thread(target=monitor.sample_loop, daemon=True).start()
    threading.Thread(target=monitor.live_loop, daemon=True).start()
    threading.Thread(target=water.watering_loop, daemon=True).start()
    threading.Thread(target=monitor.report_loop, daemon=True).start()
    log.info(f"Dashboard at http://0.0.0.0:{config.HTTP_PORT}")
    # Waitress rather than Flask's development server: it is a real WSGI server,
    # it stops the "do not use in production" warning filling the journal, and
    # its worker threads can hold long-lived connections, which the dev server
    # handles badly (that is what blocked server-sent events).
    #
    # Single process on purpose. The loops above own the PWM, the pumps and the
    # I2C bus; a second worker process would mean two controllers driving the
    # same hardware, so never run this under multiple workers.
    try:
        from waitress import serve
        serve(routes.app, host="0.0.0.0", port=config.HTTP_PORT, threads=16,
              channel_timeout=120, ident="OpenSeedling")
    except ImportError:
        log.info("waitress not installed; falling back to the Flask dev server")
        routes.app.run(host="0.0.0.0", port=config.HTTP_PORT, threaded=True)
