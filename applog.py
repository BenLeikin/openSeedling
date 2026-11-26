"""Logging for the controller.

    from applog import log
    log.warning("plug not responding: %s", err)

A warning means something is wrong but the controller carried on; an error
means an operation failed and did not happen.

Lines go to stderr as plain text that starts with the level name. Under systemd
each line also carries the journal's priority prefix, so `journalctl -u
growlight -p warning` shows warnings and errors only. GROWLIGHT_LOG_LEVEL sets
the level (default info); GROWLIGHT_LOG_FILE also writes a rotating file, for
running outside systemd.
"""

import logging
import logging.handlers
import os
import sys

LOG_NAME = "growlight"
_configured = False


# syslog priorities, as systemd reads them from a "<N>" at the start of a line
_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}


class _JournalFormatter(logging.Formatter):
    """Level, logger name and message, with a leading timestamp if with_time
    is set. With `journal`, every line starts with its priority prefix: the
    journal takes a traceback line by line, and each needs its own."""

    def __init__(self, with_time, journal=False):
        fmt = (
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
            if with_time
            else "%(levelname)-8s %(name)s: %(message)s"
        )
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self.journal = journal

    def format(self, record):
        text = super().format(record)
        if not self.journal:
            return text
        prefix = f"<{_PRIORITY.get(record.levelno, 6)}>"
        return "\n".join(prefix + line for line in text.split("\n"))


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
    stream.setFormatter(_JournalFormatter(not under_systemd, journal=under_systemd))
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


log = setup()
