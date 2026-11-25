#!/usr/bin/env python3
"""openSeedling controller: the entry point.

Starts the control loops (light, camera, sensors, watering, heat mat, daily
report) as threads and serves the dashboard on port 5000, or the port in
GROWLIGHT_HTTP_PORT. scripts/setup.sh installs what this needs and the
systemd unit that runs it.
"""

import signal
import threading

from applog import log

# Import order matters: each module sets itself up when imported. config loads
# the settings, hardware claims the pins, light starts the dither thread,
# status arms the switch watchers, routes builds the Flask app.
import config
import hardware
import light as light_mod
import setups as setups_mod  # noqa: F401
import water
import monitor
import heat
import camera as camera_mod
import status as status_mod  # noqa: F401
import routes

signal.signal(signal.SIGINT, hardware.cleanup)
signal.signal(signal.SIGTERM, hardware.cleanup)

if __name__ == "__main__":
    water.restore_persistent_state()  # before anything can water
    threading.Thread(target=light_mod.control_loop, daemon=True).start()
    threading.Thread(target=camera_mod.capture_loop, daemon=True).start()
    threading.Thread(target=monitor.sample_loop, daemon=True).start()
    threading.Thread(target=monitor.live_loop, daemon=True).start()
    threading.Thread(target=water.watering_loop, daemon=True).start()
    threading.Thread(target=monitor.report_loop, daemon=True).start()
    threading.Thread(target=heat.heat_loop, daemon=True, name="heat").start()
    log.info(f"Dashboard at http://0.0.0.0:{config.HTTP_PORT}")
    # One process only: the loops above own the PWM, the pumps and the I2C bus,
    # and a second worker would be a second controller on the same hardware.
    try:
        from waitress import serve

        serve(
            routes.app,
            host="0.0.0.0",
            port=config.HTTP_PORT,
            threads=16,
            channel_timeout=120,
            max_request_body_size=routes.MAX_BODY_BYTES,
            ident="OpenSeedling",
        )
    except ImportError:
        log.info("waitress not installed; falling back to the Flask dev server")
        routes.app.run(host="0.0.0.0", port=config.HTTP_PORT, threaded=True)
