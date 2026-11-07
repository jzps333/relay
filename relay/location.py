"""Copy the Mac's real location onto the emulator's GPS.

The emulator defaults to Google HQ in California, so "nearest Starbucks" goes
there. This reads where your Mac actually is and sends it to the emulator with
`adb emu geo fix`.

1. macOS Location Services (exact, needs a one-time "Allow" for Terminal)
2. Fallback: rough location from your internet connection (city level)

First run: macOS asks to let Terminal (or Python) use your location. Click
Allow. If you miss it: System Settings > Privacy & Security > Location Services.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

import httpx

from relay import config

log = logging.getLogger("relay.location")


def mac_location(timeout: float = 10.0) -> tuple[float, float] | None:
    """Exact (latitude, longitude) from macOS Location Services, or None."""
    try:
        import CoreLocation  # pyobjc-framework-CoreLocation (macOS only)
        from Foundation import NSDate, NSRunLoop
    except ImportError:
        return None
    manager = CoreLocation.CLLocationManager.alloc().init()
    manager.startUpdatingLocation()  # the first time, this shows the "Allow" prompt
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            # Let macOS deliver location updates to this script.
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))
            loc = manager.location()
            if loc is not None:
                c = loc.coordinate()
                return float(c.latitude), float(c.longitude)
    finally:
        manager.stopUpdatingLocation()
    return None


def ip_location() -> tuple[float, float] | None:
    """Rough (latitude, longitude) from your internet connection, or None."""
    try:
        data = httpx.get("https://ipapi.co/json/", timeout=5).json()
        return float(data["latitude"]), float(data["longitude"])
    except Exception:
        return None


def _adb() -> str:
    """Path to adb: on PATH, or Android Studio's default install folder."""
    default = os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    return shutil.which("adb") or default


def set_emulator_location(lat: float, lon: float) -> bool:
    """Move the emulator's GPS. Note geo fix takes LONGITUDE first."""
    cmd = [_adb()]
    if config.ADB_SERIAL:
        cmd += ["-s", config.ADB_SERIAL]
    cmd += ["emu", "geo", "fix", f"{lon:.6f}", f"{lat:.6f}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("geo fix failed: %r", e)
        return False
    ok = result.returncode == 0 and "KO" not in (result.stdout + result.stderr)
    log.info("emulator location -> %.5f, %.5f (%s)", lat, lon, "ok" if ok else result.stdout.strip())
    return ok


_synced_at = 0.0


def sync_emulator_to_mac(max_age: float = 600.0) -> bool:
    """Copy the Mac's location to the emulator (at most once every `max_age` seconds)."""
    global _synced_at
    if not config.SYNC_MAC_LOCATION:
        return False
    if time.monotonic() - _synced_at < max_age and _synced_at:
        return True
    where = mac_location() or ip_location()
    if where is None:
        log.info("couldn't find the Mac's location")
        return False
    if set_emulator_location(*where):
        _synced_at = time.monotonic()
        return True
    return False


if __name__ == "__main__":
    # `python -m relay.location` : check it works and see where it thinks you are.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    exact = mac_location()
    print("Mac Location Services:", exact or "not available (allow Terminal in Location Services)")
    if exact is None:
        print("Internet-based guess:", ip_location())
    print("Emulator updated:", sync_emulator_to_mac(max_age=0))
