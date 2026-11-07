from types import SimpleNamespace

from relay import config, location


def test_geo_fix_sends_longitude_first(monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "ADB_SERIAL", "emulator-5554")
    monkeypatch.setattr(location, "_adb", lambda: "adb")

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr(location.subprocess, "run", fake_run)
    assert location.set_emulator_location(37.5407, -77.4360)
    assert seen["cmd"] == ["adb", "-s", "emulator-5554", "emu", "geo", "fix", "-77.436000", "37.540700"]


def test_sync_falls_back_to_ip_and_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(location, "_synced_at", 0.0)
    monkeypatch.setattr(location, "mac_location", lambda: None)
    monkeypatch.setattr(location, "ip_location", lambda: (37.5, -77.4))
    got = []
    monkeypatch.setattr(location, "set_emulator_location", lambda lat, lon: got.append((lat, lon)) or True)
    monkeypatch.setattr(config, "SYNC_MAC_LOCATION", True)
    assert location.sync_emulator_to_mac(max_age=0)
    assert got == [(37.5, -77.4)]
    monkeypatch.setattr(config, "SYNC_MAC_LOCATION", False)
    assert not location.sync_emulator_to_mac(max_age=0)
