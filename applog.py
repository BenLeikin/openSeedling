"""Logging for the controller.

    from applog import log
    log.warning("plug not responding: %s", err)

A warning means something is wrong but the controller carried on; an error
means an operation failed and did not happen.

Lines go to stderr as plain text that starts with the level name. Under systemd
the journal captures them, all at one journal priority, so filter on the level
name in the text. GROWLIGHT_LOG_LEVEL sets the level (default info);
GROWLIGHT_LOG_FILE also writes a rotating file, for running outside systemd.
"""

import logging
import logging.handlers
import os
import sys

LOG_NAME = "growlight"
_configured = False


class _JournalFormatter(logging.Formatter):
    """Level, logger name and message, with a leading timestamp if with_time is set."""

    def __init__(self, with_time):
        fmt = (
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
            if with_time
            else "%(levelname)-8s %(name)s: %(message)s"
        )
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")


def setup(level=None, logfile=None, force=False):
    """Configure logging once. Safe to call from any module, in any order."""
    global _configured
    if _configured and not force:
        return logging.getLogger(LOG_NAME)

    lvl = (level or os.environ.get("GROWLIGHT_LOG_LEVEL") or "info").upper()
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    # the journal records the time of every line itself
    under_systemd = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_JournalFormatter(not under_systemd))
    logger.addHandler(stream)

    path = logfile or os.environ.get("GROWLIGHT_LOG_FILE")
    if path:
        try:
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
            fh.setFormatter(_JournalFormatter(True))
            logger.addHandler(fh)
        except OSError as e:
            # a log file that cannot be opened must not stop the controller
            logger.warning("log file %s unavailable: %s", path, e)

    _configured = True
    return logger


def get(module=None):
    """A logger for one module: applog.get(__name__) -> growlight.sensors."""
    setup()
    if not module:
        return logging.getLogger(LOG_NAME)
    short = module.rsplit(".", 1)[-1]
    return logging.getLogger(f"{LOG_NAME}.{short}")


log = setup()
