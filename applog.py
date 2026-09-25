"""Logging for the controller.

Everything used to go to stdout with print(), which systemd captured into the
journal. That worked, but every line arrived at the same priority: a pump
refusing to run and a routine sample looked identical, and there was no way to
ask for "only the problems" without grepping for words.

This keeps that simplicity, in that a module still writes one line and does not
care where it goes, but adds levels and a consistent prefix:

    from applog import log
    log.info("fill tray %s complete in %.1fs", tray, elapsed)
    log.warning("plug not responding: %s", err)
    log.exception("sample loop failed")     # inside an except: adds traceback

Levels, and what belongs at each:

    debug     per-sample detail, only useful while chasing something
    info      things that happened and were meant to: fills, light changes
    warning   something is wrong but the controller carried on: a sensor gone
              quiet, a plug that did not answer, a setting that would not save
    error     an operation failed and did not happen: a pump that refused, a
              capture that produced nothing
    critical  the controller cannot continue

Output goes to stderr, which systemd routes to the journal with the level
preserved, so `journalctl -u growlight -p warning` shows only what matters.
GROWLIGHT_LOG_LEVEL overrides the level (default info); GROWLIGHT_LOG_FILE
additionally writes a rotating file, for running outside systemd.
"""
import logging
import logging.handlers
import os
import sys

LOG_NAME = "growlight"
_configured = False


class _JournalFormatter(logging.Formatter):
    """Plain lines under systemd, timestamped lines everywhere else.

    The journal already records the time and the unit, so repeating them wastes
    width in every line. Outside systemd there is nothing else keeping time, so
    the timestamp goes in.
    """

    def __init__(self, with_time):
        fmt = ("%(asctime)s %(levelname)-8s %(name)s: %(message)s" if with_time
               else "%(levelname)-8s %(name)s: %(message)s")
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

    # under systemd the journal timestamps every line already
    under_systemd = bool(os.environ.get("JOURNAL_STREAM")
                         or os.environ.get("INVOCATION_ID"))
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_JournalFormatter(not under_systemd))
    logger.addHandler(stream)

    path = logfile or os.environ.get("GROWLIGHT_LOG_FILE")
    if path:
        try:
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=3)
            fh.setFormatter(_JournalFormatter(True))
            logger.addHandler(fh)
        except OSError as e:
            # a log file that cannot be opened must never stop the controller
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
