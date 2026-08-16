"""Raspberry Pi host telemetry for the dashboard.

Everything is read from /proc, /sys and vcgencmd, so there are no extra
dependencies and nothing here can block: each reader is individually guarded
and returns None when its source is unavailable, which keeps the module usable
off-Pi (and in tests) without special-casing.

The one non-obvious reading is the throttle word from `vcgencmd get_throttled`.
It is a bitmask where the low bits mean "happening now" and the high bits mean
"has happened since boot". Undervoltage is the single most common cause of
mysterious Pi misbehaviour, so it is worth surfacing plainly rather than
leaving it to be discovered in dmesg.
"""

import os
import re
import shutil
import socket
import subprocess
import time

# bit -> (now_label, since_boot_label)
THROTTLE_BITS = {
    0:  "undervoltage",
    1:  "arm frequency capped",
    2:  "currently throttled",
    3:  "soft temperature limit",
}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def cpu_temp_c():
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if not raw:
        return None
    try:
        return round(int(raw) / 1000.0, 1)
    except ValueError:
        return None


def load_avg():
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": round(one, 2), "5m": round(five, 2),
                "15m": round(fifteen, 2), "cores": os.cpu_count() or 1}
    except Exception:
        return None


def memory():
    raw = _read("/proc/meminfo")
    if not raw:
        return None
    vals = {}
    for line in raw.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            vals[m.group(1)] = int(m.group(2))       # kB
    total = vals.get("MemTotal")
    avail = vals.get("MemAvailable")
    if not total:
        return None
    used = total - (avail if avail is not None else 0)
    return {"used_mb": round(used / 1024), "total_mb": round(total / 1024),
            "percent": round(used / total * 100)}


def disk(path="/"):
    try:
        t, u, f = shutil.disk_usage(path)
        return {"used_gb": round(u / 1e9, 1), "total_gb": round(t / 1e9, 1),
                "percent": round(u / t * 100)}
    except Exception:
        return None


def uptime_seconds():
    raw = _read("/proc/uptime")
    if not raw:
        return None
    try:
        return int(float(raw.split()[0]))
    except (ValueError, IndexError):
        return None


def _vcgencmd(arg):
    try:
        r = subprocess.run(["vcgencmd", arg], capture_output=True, timeout=3)
        if r.returncode != 0:
            return None
        return r.stdout.decode(errors="replace").strip()
    except Exception:
        return None


def throttled():
    """Power/thermal health. Returns a dict with the current and since-boot
    conditions, or None when vcgencmd is unavailable."""
    out = _vcgencmd("get_throttled")
    if not out or "=" not in out:
        return None
    try:
        word = int(out.split("=")[1], 0)
    except (ValueError, IndexError):
        return None
    now, ever = [], []
    for bit, label in THROTTLE_BITS.items():
        if word & (1 << bit):
            now.append(label)
        if word & (1 << (bit + 16)):
            ever.append(label)
    return {"raw": word, "now": now, "since_boot": ever,
            "ok": not now and not ever}


def core_voltage():
    out = _vcgencmd("measure_volts core")
    if not out:
        return None
    m = re.search(r"([\d.]+)V", out)
    return round(float(m.group(1)), 4) if m else None


def cpu_mhz():
    raw = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if raw:
        try:
            return round(int(raw) / 1000)
        except ValueError:
            pass
    out = _vcgencmd("measure_clock arm")
    if out and "=" in out:
        try:
            return round(int(out.split("=")[1]) / 1_000_000)
        except (ValueError, IndexError):
            return None
    return None


def wifi():
    """Signal quality from /proc/net/wireless, plus the interface name."""
    raw = _read("/proc/net/wireless")
    if not raw:
        return None
    for line in raw.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        iface = parts[0].rstrip(":")
        try:
            quality = float(parts[2])        # out of 70 on most drivers
            level = float(parts[3])          # dBm
        except ValueError:
            continue
        return {"iface": iface, "percent": max(0, min(100, round(quality / 70 * 100))),
                "dbm": round(level)}
    return None


def host():
    try:
        return socket.gethostname()
    except Exception:
        return None


def ip_address():
    """The address actually used for outbound traffic, without shelling out."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 1))          # TEST-NET-1: never routed
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        if s:
            s.close()


def model():
    raw = _read("/proc/device-tree/model")
    return raw.replace("\x00", "").strip() if raw else None


def all_stats():
    """Everything the dashboard shows, in one call."""
    return {
        "cpu_temp_c": cpu_temp_c(),
        "load": load_avg(),
        "memory": memory(),
        "disk": disk(),
        "uptime_seconds": uptime_seconds(),
        "throttled": throttled(),
        "core_voltage": core_voltage(),
        "cpu_mhz": cpu_mhz(),
        "wifi": wifi(),
        "host": host(),
        "ip": ip_address(),
        "model": model(),
        "ts": int(time.time()),
    }


if __name__ == "__main__":
    for k, v in all_stats().items():
        print(f"  {k:16s} {v}")
