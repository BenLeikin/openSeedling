"""Readings are stored in Celsius and hPa; these convert them to the units
chosen in Settings for display."""

import config


def display_units():
    with config.settings_lock:
        return config.settings.get("units", "imperial")


def temp_out(c):
    """Celsius storage -> the configured display unit."""
    return round(c if display_units() == "metric" else c * 9 / 5 + 32, 1)


def temp_unit():
    return "C" if display_units() == "metric" else "F"


def press_out(hpa):
    return round(
        hpa if display_units() == "metric" else hpa * 0.0295299830714,
        0 if display_units() == "metric" else 2,
    )


def press_unit():
    return "hPa" if display_units() == "metric" else "inHg"
