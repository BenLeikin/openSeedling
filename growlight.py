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

# Importing these starts the app: each module sets itself up when imported.
# config loads the settings, hardware claims the pins, light starts its dither
# thread, status arms the switch watchers, routes builds the Flask app.
import auth
import config
import hardware
import control
import water
import monitor
import heat
import camera as camera_mod
import routes
import routes_camera
import routes_climate
import routes_data
import routes_garden

for _area in (routes_camera, routes_garden, routes_climate, routes_data):
    routes.app.register_blueprint(_area.bp)

signal.signal(signal.SIGINT, hardware.cleanup)
signal.signal(signal.SIGTERM, hardware.cleanup)

if __name__ == "__main__":
    water.restore_persistent_state()  # before anything can water
    threading.Thread(target=control.control_loop, daemon=True).start()
    threading.Thread(target=camera_mod.capture_loop, daemon=True).start()
    threading.Thread(target=monitor.sample_loop, daemon=True).start()
    threading.Thread(target=monitor.live_loop, daemon=True).start()
    threading.Thread(target=water.watering_loop, daemon=True).start()
    threading.Thread(target=monitor.report_loop, daemon=True).start()
    threading.Thread(target=heat.heat_loop, daemon=True, name="heat").start()
    log.info(f"Dashboard at http://0.0.0.0:{config.HTTP_PORT}")
    if config.TRUST_LOCALHOST:
        log.info(f"touchscreen listener at http://127.0.0.1:{config.SCREEN_PORT}")
    proxy = config.proxy_options()
    if proxy:
        log.info(f"trusting forwarded headers from the proxy at {proxy['trusted_proxy']}")
    if config.login_https_only():
        log.info("sign-in is accepted over HTTPS only")
    elif config.LOGIN_HTTPS_ONLY:
        log.error(
            "GROWLIGHT_LOGIN_HTTPS_ONLY is set without a usable GROWLIGHT_TRUSTED_PROXY, "
            "so nothing arrives over HTTPS; sign-in stays open on plain http"
        )
    # One process only: the loops above own the PWM, the pumps and the I2C bus,
    # and a second worker would be a second controller on the same hardware.
    try:
        from waitress import serve

        serve(
            routes.app,
            listen=config.listen_on(),
            threads=16,
            channel_timeout=120,
            max_request_body_size=auth.MAX_BODY_BYTES,
            ident="OpenSeedling",
            **proxy,
        )
    except ImportError:
        log.info("waitress not installed; falling back to the Flask dev server")
        if config.TRUST_LOCALHOST:
            log.warning("the dev server has one port: the touchscreen will not be signed in")
        routes.app.run(host="0.0.0.0", port=config.HTTP_PORT, threaded=True)
