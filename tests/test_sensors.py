"""Tests: sensors."""

import pytest
from harness import db, sensors


@pytest.fixture
def w1_bus(tmp_path, monkeypatch):
    """A 1-Wire bus in a temp folder: plug(serial, celsius) and unplug(serial)."""
    monkeypatch.setattr(sensors, "W1_PROBES", str(tmp_path / "28-*"))
    monkeypatch.setattr(sensors, "_soil_named_by_serial", None)
    monkeypatch.setitem(sensors.ENABLED, "soil_temp", True)
    db.kv_set(sensors.SOIL_BY_SERIAL_KEY, False)

    class Bus:
        def plug(self, serial, celsius):
            d = tmp_path / f"28-{serial}"
            d.mkdir(exist_ok=True)
            (d / "w1_slave").write_text(
                f"aa bb : crc=cc YES\naa bb t={int(celsius * 1000)}\n", encoding="ascii"
            )

        def unplug(self, serial):
            d = tmp_path / f"28-{serial}"
            (d / "w1_slave").unlink()
            d.rmdir()

    yield Bus()
    db.kv_set(sensors.SOIL_BY_SERIAL_KEY, False)


def test_a_soil_probe_keeps_its_name_when_the_other_drops_out(w1_bus, monkeypatch):
    w1_bus.plug("00000000aaaa", 21.5)
    assert sensors._read_soil_temps() == {"temp:soil": 21.5}, "one probe is plain temp:soil"

    w1_bus.plug("00000000bbbb", 23.0)
    both = sensors._read_soil_temps()
    assert both == {"temp:soil_aaaa": 21.5, "temp:soil_bbbb": 23.0}

    w1_bus.unplug("00000000aaaa")
    assert sensors._read_soil_temps() == {"temp:soil_bbbb": 23.0}, (
        "the probe that is left keeps its name, so its history and the heat mat's sensor hold"
    )
    monkeypatch.setattr(sensors, "_soil_named_by_serial", None)  # the service restarts
    assert sensors._read_soil_temps() == {"temp:soil_bbbb": 23.0}, "and across a restart"
