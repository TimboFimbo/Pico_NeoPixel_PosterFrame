#!/usr/bin/env python3
import os
import time
import requests
from dataclasses import dataclass
from typing import Optional, Tuple

# Optional .env support
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
    print("ENV CHECK:", os.getenv("JELLYFIN_BASE"))
except Exception:
    pass

# Optional NPS control (MOSFET) via gpiozero
NPS_AVAILABLE = False
try:
    from gpiozero import PWMLED
    NPS_AVAILABLE = True
except Exception:
    NPS_AVAILABLE = False


@dataclass
class PlaybackSnapshot:
    state: str  # "playing" | "paused" | "stopped"
    pct: float  # 0..1
    item_id: Optional[str]
    title: str


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


JELLYFIN_BASE = env_required("JELLYFIN_BASE").rstrip("/")  # e.g. http://192.168.0.118:8096
JELLYFIN_API_KEY = env_required("JELLYFIN_API_KEY")
JELLYFIN_DEVICE_NAME = env_required("JELLYFIN_DEVICE_NAME")  # Katie's 2nd FireTVStick

PICO_BASE = env_required("PICO_BASE").rstrip("/")  # e.g. http://192.168.0.50
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2.0"))

# NPS config (optional)
NPS_ENABLED = os.getenv("NPS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
NPS_GPIO = int(os.getenv("NPS_GPIO", "18"))  # BCM pin (GPIO18 supports hardware PWM, but gpiozero will soft-PWM too)

# How fast to breathe on pause
NPS_FADE_PERIOD_S = float(os.getenv("NPS_FADE_PERIOD_S", "2.5"))

# Some clients report quirky values; clamp sanity
MIN_DURATION_TICKS = 10_000_000  # 1 second in ticks is 10,000,000; so this is ~1s threshold


def jellyfin_headers():
    # Jellyfin commonly accepts X-MediaBrowser-Token
    return {"X-MediaBrowser-Token": JELLYFIN_API_KEY}


def http_get_json(url: str, headers=None, timeout=4):
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def pico_get_text(path: str, timeout=3) -> str:
    r = requests.get(PICO_BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.text


def pico_get_json(path: str, timeout=3) -> dict:
    r = requests.get(PICO_BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def pico_call(path: str, timeout=3) -> None:
    # We don't really care about the response body; just ensure request completes
    r = requests.get(PICO_BASE + path, timeout=timeout)
    r.raise_for_status()


def find_target_session(sessions: list, device_name: str) -> Optional[dict]:
    # DeviceName is usually present; some clients might use "DeviceName" or "Device"
    for s in sessions:
        dn = (s.get("DeviceName") or s.get("Device") or "").strip()
        if dn == device_name:
            return s
    return None


def snapshot_from_session(sess: Optional[dict]) -> PlaybackSnapshot:
    if not sess:
        return PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")

    now_item = sess.get("NowPlayingItem")
    play_state = sess.get("PlayState") or {}

    # If no NowPlayingItem, treat as stopped
    if not now_item:
        return PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")

    item_id = now_item.get("Id")
    title = (now_item.get("Name") or "")
    series = (now_item.get("SeriesName") or "")
    season = (now_item.get("SeasonName") or "")

    # Build a friendly title string
    pretty = " - ".join([x for x in [series, season, title] if x])

    duration = int(now_item.get("RunTimeTicks") or 0)
    position = int(play_state.get("PositionTicks") or 0)
    is_paused = bool(play_state.get("IsPaused"))

    # Defensive: sometimes duration is missing or 0
    if duration < MIN_DURATION_TICKS:
        pct = 0.0
    else:
        pct = max(0.0, min(1.0, position / float(duration)))

    state = "paused" if is_paused else "playing"
    return PlaybackSnapshot(state=state, pct=pct, item_id=item_id, title=pretty)


class NpsController:
    """
    MOSFET low-side switch driven by PWM.
    - playing: solid ON
    - paused: breathing
    - stopped: OFF
    """
    def __init__(self):
        self.enabled = NPS_ENABLED and NPS_AVAILABLE
        self._led = PWMLED(NPS_GPIO) if self.enabled else None
        self._mode = "stopped"
        self._t0 = time.monotonic()

    def set_state(self, state: str):
        if not self.enabled:
            return
        if state != self._mode:
            self._mode = state
            self._t0 = time.monotonic()

    def tick(self):
        if not self.enabled:
            return
        if self._mode == "playing":
            self._led.value = 1.0
        elif self._mode == "paused":
            # sine breathe 0.15..1.0
            t = time.monotonic() - self._t0
            w = (1.0 + __import__("math").sin((2.0 * __import__("math").pi * t) / NPS_FADE_PERIOD_S)) * 0.5
            self._led.value = 0.15 + 0.85 * w
        else:
            self._led.value = 0.0


def main():
    print("Jellyfin → Pico bridge starting")
    print("Jellyfin:", JELLYFIN_BASE)
    print("Target device:", JELLYFIN_DEVICE_NAME)
    print("Pico:", PICO_BASE)
    print("Poll seconds:", POLL_SECONDS)
    if NPS_ENABLED:
        print("NPS:", "enabled" if NPS_AVAILABLE else "requested but gpiozero missing")

    nps = NpsController()

    last = PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")
    last_progress_mode_enabled = None

    while True:
        try:
            # Check if progress mode is enabled on the Pico
            pico_status = pico_get_json("/api/status", timeout=3)
            progress_mode_enabled = bool(pico_status.get("progress_mode_enabled", False))
            last_progress_mode_enabled = progress_mode_enabled

            # Poll Jellyfin sessions
            sessions = http_get_json(JELLYFIN_BASE + "/Sessions", headers=jellyfin_headers(), timeout=5)
            target = find_target_session(sessions, JELLYFIN_DEVICE_NAME)
            snap = snapshot_from_session(target)

            # NPS control (doesn't depend on progress mode toggle; you can change this later)
            nps.set_state(snap.state if snap.state in ("playing", "paused") else "stopped")
            nps.tick()

            # If Pico progress mode is OFF, do nothing else (leave lights in regular mode)
            if not progress_mode_enabled:
                last = snap
                time.sleep(POLL_SECONDS)
                continue

            # Progress mode ON: translate transitions into Pico events
            # Item changed or stopped->playing counts as start
            item_changed = (snap.item_id is not None and snap.item_id != last.item_id)

            if snap.state == "playing":
                if last.state != "playing" or item_changed:
                    pico_call("/api/event?name=movie_start", timeout=3)
                pico_call(f"/api/progress?pct={snap.pct:.6f}&state=playing", timeout=3)

            elif snap.state == "paused":
                # If we just transitioned into paused
                if last.state != "paused":
                    pico_call("/api/event?name=movie_pause", timeout=3)
                pico_call(f"/api/progress?pct={snap.pct:.6f}&state=paused", timeout=3)

            else:
                # stopped
                if last.state != "stopped":
                    pico_call("/api/event?name=movie_stop", timeout=3)
                pico_call("/api/progress?state=stopped", timeout=3)

            # Light logging for sanity
            if snap.state != last.state or item_changed:
                print(f"[{time.strftime('%H:%M:%S')}] {snap.state.upper()} pct={snap.pct:.3f} title={snap.title}")

            last = snap
            time.sleep(POLL_SECONDS)

        except Exception as e:
            # Keep running even if Jellyfin/Pico is temporarily unreachable
            print("Bridge error:", repr(e))
            try:
                # Keep NPS ticking (so pause breathe continues)
                nps.tick()
            except Exception:
                pass
            time.sleep(max(1.0, POLL_SECONDS))


if __name__ == "__main__":
    main()
