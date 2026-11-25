"""Readings are stored in Celsius and hPa; these convert them to the units
chosen in Settings for display."""

import config


def display_units() -> str:
    with config.settings_lock:
        return str(config.settings.get("units", "imperial"))


def temp_out(c: float) -> float:
    """Celsius storage -> the configured display unit."""
    return round(c if display_units() == "metric" else c * 9 / 5 + 32, 1)


def temp_unit() -> str:
    return "C" if display_units() == "metric" else "F"


def press_out(hpa: float) -> float:
    return round(
        hpa if display_units() == "metric" else hpa * 0.0295299830714,
        0 if display_units() == "metric" else 2,
    )


def press_unit() -> str:
    return "hPa" if display_units() == "metric" else "inHg"
