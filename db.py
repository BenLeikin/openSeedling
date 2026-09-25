#!/usr/bin/env python3
"""SQLite logging layer for the grow dashboard.

One Pi, one writer: SQLite in WAL mode is plenty and needs no daemon.

Schema (long / time-series format so new sensors never require migrations):
  readings(ts, sensor, value)          raw samples, kept ~30 days
  readings_hourly(ts, sensor, value)   hourly averages, kept long-term
  events(ts, type, detail)             discrete happenings

Sensor keys are namespaced strings, e.g. "moisture:B2", "temp:soil_east",
"humidity", "lux". All timestamps are Unix epoch seconds, UTC.

Usage:
  import db
  db.init()                                  # once at startup
  db.log_many([("moisture:B2", 43.1), ...])  # one transaction per cycle
  db.log_event("pump", "ran 8s")
  rows = db.series("moisture:B2", hours=168)  # last 7 days
  db.downsample_and_prune()                   # daily housekeeping
"""

import json
import sqlite3
import time
from pathlib import Path

from applog import log     # levelled logging; see applog.py

DB_PATH = Path(__file__).with_name("growlight.db")

RAW_RETENTION_DAYS = 30   # raw samples older than this are rolled up + deleted

_conn = None


def _c():
    """Module-level connection, opened lazily. check_same_thread=False because
    Flask serves from multiple threads; WAL keeps readers and the writer happy."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, fewer SD flushes
    return _conn


def init():
    c = _c()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            ts     INTEGER NOT NULL,
            sensor TEXT    NOT NULL,
            value  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts
            ON readings(sensor, ts);

        CREATE TABLE IF NOT EXISTS readings_hourly (
            ts     INTEGER NOT NULL,   -- start of the hour, UTC
            sensor TEXT    NOT NULL,
            value  REAL    NOT NULL,
            PRIMARY KEY (sensor, ts)
        );

        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            ts    INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            ts     INTEGER NOT NULL,
            type   TEXT    NOT NULL,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

        -- Finished plantings. A cell is cleared when its plant is transplanted
        -- or dies, so this is where the germination record actually lives:
        -- without it, every transplant would silently delete a data point from
        -- the variety's success rate.
        CREATE TABLE IF NOT EXISTS plantings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        INTEGER NOT NULL,   -- when the record was written
            tray      TEXT    NOT NULL,
            cell      TEXT    NOT NULL,
            seed      TEXT,
            equipment TEXT,
            planted   TEXT,               -- ISO dates, as the tray stores them
            sprouted  TEXT,
            ended     TEXT,
            outcome   TEXT    NOT NULL,   -- 'transplanted' | 'died'
            count     INTEGER,
            source    TEXT,
            notes     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_plantings_ts ON plantings(ts);
    """)
    c.commit()


# ----------------------------- writing -----------------------------

def log_many(pairs, ts=None):
    """Insert several (sensor, value) readings in one transaction.
    Skips Nones so a single failed sensor doesn't abort the batch."""
    ts = int(ts if ts is not None else time.time())
    rows = [(ts, str(s), float(v)) for s, v in pairs if v is not None]
    if not rows:
        return
    c = _c()
    with c:  # transaction
        c.executemany("INSERT INTO readings(ts, sensor, value) VALUES (?,?,?)", rows)


def log_reading(sensor, value, ts=None):
    log_many([(sensor, value)], ts=ts)


def kv_set(key, obj):
    """Store a small piece of runtime state that must survive a restart.

    For state that lives in memory while the app runs but that a restart must
    not forget: the pump's daily total and last run time are what enforce the
    daily cap and the auto-water cooldown, so losing them on a restart would
    quietly loosen both.
    """
    c = _c()
    with c:
        c.execute("INSERT INTO kv(key, value, ts) VALUES (?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                  "ts=excluded.ts",
                  (str(key), json.dumps(obj), int(time.time())))


def kv_get(key, default=None):
    """Read state stored with kv_set; default if absent or unreadable."""
    try:
        row = _c().execute("SELECT value FROM kv WHERE key=?",
                           (str(key),)).fetchone()
    except sqlite3.Error:
        return default
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def log_event(etype, detail="", ts=None):
    ts = int(ts if ts is not None else time.time())
    c = _c()
    with c:
        c.execute("INSERT INTO events(ts, type, detail) VALUES (?,?,?)",
                  (ts, str(etype), str(detail)))


# ----------------------------- reading -----------------------------

def series(sensor, hours=168):
    """Return [(ts, value), ...] for a sensor over the last `hours`.
    Pulls hourly rollups for the old part and raw for the recent part,
    so a long window stays light."""
    since = int(time.time()) - hours * 3600
    c = _c()
    raw = c.execute(
        "SELECT ts, value FROM readings WHERE sensor=? AND ts>=? ORDER BY ts",
        (sensor, since)).fetchall()
    hourly = c.execute(
        "SELECT ts, value FROM readings_hourly WHERE sensor=? AND ts>=? ORDER BY ts",
        (sensor, since)).fetchall()
    merged = {r["ts"]: r["value"] for r in hourly}
    merged.update({r["ts"]: r["value"] for r in raw})  # raw wins where overlapping
    return sorted(merged.items())


def latest(sensors=None, max_age_days=7):
    """Most recent value per sensor -> {sensor: (ts, value)}.

    Bounded to the last `max_age_days`: this query runs on every status poll,
    and an unbounded GROUP BY walks the whole 30-day raw table each time. A
    sensor silent for over a week has no useful "latest" anyway (the stale
    alert covers telling you it died)."""
    since = int(time.time()) - max_age_days * 86400
    c = _c()
    rows = c.execute("""
        SELECT r.sensor, r.ts, r.value FROM readings r
        JOIN (SELECT sensor, MAX(ts) ts FROM readings
              WHERE ts >= ? GROUP BY sensor) m
          ON r.sensor=m.sensor AND r.ts=m.ts
    """, (since,)).fetchall()
    out = {r["sensor"]: (r["ts"], r["value"]) for r in rows}
    if sensors is not None:
        out = {k: v for k, v in out.items() if k in sensors}
    return out


def add_planting(rec):
    """Record a finished planting. Returns its row id."""
    c = _c()
    cur = c.execute(
        "INSERT INTO plantings (ts, tray, cell, seed, equipment, planted, "
        "sprouted, ended, outcome, count, source, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), str(rec.get("tray", "")), str(rec.get("cell", "")),
         rec.get("seed") or "", rec.get("equipment") or "",
         rec.get("planted") or "", rec.get("sprouted") or "",
         rec.get("ended") or "", rec.get("outcome") or "transplanted",
         int(rec.get("count") or 0), rec.get("source") or "",
         rec.get("notes") or ""))
    c.commit()
    return cur.lastrowid


def plantings(limit=500):
    """Finished plantings, newest first."""
    cols = ("id", "ts", "tray", "cell", "seed", "equipment", "planted",
            "sprouted", "ended", "outcome", "count", "source", "notes")
    rows = _c().execute(
        f"SELECT {', '.join(cols)} FROM plantings ORDER BY ts DESC, id DESC "
        "LIMIT ?", (int(limit),)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def delete_planting(pid):
    """Remove one history row (used when a cell is restored)."""
    c = _c()
    n = c.execute("DELETE FROM plantings WHERE id=?", (int(pid),)).rowcount
    c.commit()
    return n


def recent_events(limit=50):
    c = _c()
    rows = c.execute(
        "SELECT ts, type, detail FROM events ORDER BY ts DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def recent_values(sensor, n=9, max_age=7200):
    """The last `n` raw values for `sensor`, newest first, ignoring anything
    older than `max_age` seconds. For filtering a live reading: a median over
    these rejects a single transient without waiting for a long window."""
    since = int(time.time()) - max_age
    return [r[0] for r in _c().execute(
        "SELECT value FROM readings WHERE sensor=? AND ts>=? "
        "ORDER BY ts DESC LIMIT ?", (sensor, since, max(1, int(n))))]


def reading_near(sensor, ts, window=3600):
    """Value for a sensor closest to time `ts` (for timelapse-frame labels).
    Returns None if nothing within `window` seconds."""
    c = _c()
    row = c.execute("""
        SELECT value, ABS(ts-?) d FROM readings
        WHERE sensor=? AND ABS(ts-?)<=? ORDER BY d LIMIT 1
    """, (ts, sensor, ts, window)).fetchone()
    return row["value"] if row else None


# --------------------------- housekeeping ---------------------------

def delete_series_prefix(prefix):
    """Remove every reading (raw and hourly) whose sensor starts with `prefix`.
    Returns the number of raw rows deleted."""
    c = _c()
    # % and _ are LIKE wildcards; escape them instead of stripping them, or a
    # prefix like "growth_px:" becomes "growthpx:%" and matches nothing
    esc = (prefix.replace("\\", "\\\\").replace("%", "\\%")
                 .replace("_", "\\_")) + "%"
    n = c.execute("DELETE FROM readings WHERE sensor LIKE ? ESCAPE '\\'",
                  (esc,)).rowcount
    c.execute("DELETE FROM readings_hourly WHERE sensor LIKE ? ESCAPE '\\'",
              (esc,))
    c.commit()
    return n


def downsample_and_prune():
    """Roll raw readings older than RAW_RETENTION_DAYS into hourly averages,
    then delete those raw rows. Idempotent; safe to run daily."""
    cutoff = int(time.time()) - RAW_RETENTION_DAYS * 86400
    c = _c()
    with c:
        c.execute("""
            INSERT OR REPLACE INTO readings_hourly(ts, sensor, value)
            SELECT (ts/3600)*3600 AS hr, sensor, AVG(value)
            FROM readings WHERE ts < ?
            GROUP BY hr, sensor
        """, (cutoff,))
        c.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")


if __name__ == "__main__":
    # Self-test: log fake data, query it, exercise housekeeping. No hardware needed.
    import random
    init()
    now = int(time.time())
    log.info("seeding 3 days of fake data for moisture:B2 ...")
    for i in range(3 * 24 * 12):                 # every 5 min for 3 days
        t = now - i * 300
        log_reading("moisture:B2", 40 + 10 * random.random(), ts=t)
    log_event("pump", "ran 8s (self-test)")
    s = series("moisture:B2", hours=72)
    log.info(f"series points: {len(s)}  first={s[0]}  last={s[-1]}")
    log.info(f"latest: {latest()}")
    log.info(f"events: {recent_events(3)}")
    log.info(f"near now: {reading_near('moisture:B2', now)}")
    downsample_and_prune()
    log.info(f"after prune, raw points 72h: {len(series('moisture:B2', hours=72))}")
    log.info("OK")
