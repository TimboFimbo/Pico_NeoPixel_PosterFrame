#!/usr/bin/env python3
import os
import time
import math
import requests
from dataclasses import dataclass
from typing import Optional

# --- .env loading (explicit path beside script) ---
try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(dotenv_path=Path(__file__).with_name(".env"))
except Exception:
    # If python-dotenv isn't installed, env vars must come from systemd EnvironmentFile
    pass

# --- Optional NPS control via gpiozero ---
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


# --- Config from .env ---
JELLYFIN_BASE = env_required("JELLYFIN_BASE").rstrip("/")          # e.g. http://192.168.0.118:8096
JELLYFIN_API_KEY = env_required("JELLYFIN_API_KEY")
JELLYFIN_DEVICE_NAME = env_required("JELLYFIN_DEVICE_NAME")        # Katie's 2nd FireTVStick

PICO_BASE = env_required("PICO_BASE").rstrip("/")                  # e.g. http://192.168.0.250

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2.0"))             # Jellyfin poll cadence
PICO_STATUS_SECONDS = float(os.getenv("PICO_STATUS_SECONDS", "10"))# How often to check Pico status
SESSION_GRACE_S = float(os.getenv("SESSION_GRACE_S", "10"))         # Hold last session state this long if session disappears briefly

# NPS (MOSFET) control
NPS_ENABLED = os.getenv("NPS_ENABLED", "1").lower() in ("1", "true", "yes", "on")
NPS_GPIO = int(os.getenv("NPS_GPIO", "18"))
NPS_FADE_PERIOD_S = float(os.getenv("NPS_FADE_PERIOD_S", "2.8"))    # Full cycle ~2.8s

# Defensive for missing runtimes
MIN_DURATION_TICKS = 10_000_000  # ~1 second (Jellyfin ticks are 10,000,000 per second)


def make_session() -> requests.Session:
    s = requests.Session()
    # Basic retries (helps with occasional transient failures)
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def jellyfin_headers():
    # Jellyfin supports this token header for API key auth
    return {"X-MediaBrowser-Token": JELLYFIN_API_KEY}


def jellyfin_sessions(sess: requests.Session) -> list:
    r = sess.get(JELLYFIN_BASE + "/Sessions", headers=jellyfin_headers(), timeout=5.0)
    r.raise_for_status()
    return r.json()


def pico_get_json(sess: requests.Session, path: str, timeout=4.0) -> dict:
    r = sess.get(PICO_BASE + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


def pico_call(sess: requests.Session, path: str, timeout=4.0) -> None:
    r = sess.get(PICO_BASE + path, timeout=timeout)
    r.raise_for_status()


def find_target_session(sessions: list, device_name: str) -> Optional[dict]:
    # Tolerant matching avoids flicker due to whitespace/case changes
    target = device_name.strip().casefold()
    for s in sessions:
        dn = (s.get("DeviceName") or s.get("Device") or "").strip().casefold()
        if dn == target:
            return s
    return None


def snapshot_from_session(sess: Optional[dict]) -> PlaybackSnapshot:
    if not sess:
        return PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")

    now_item = sess.get("NowPlayingItem")
    play_state = sess.get("PlayState") or {}
    if not now_item:
        return PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")

    item_id = now_item.get("Id")
    title = (now_item.get("Name") or "")
    series = (now_item.get("SeriesName") or "")
    season = (now_item.get("SeasonName") or "")
    pretty = " - ".join([x for x in [series, season, title] if x])

    duration = int(now_item.get("RunTimeTicks") or 0)
    position = int(play_state.get("PositionTicks") or 0)
    is_paused = bool(play_state.get("IsPaused"))

    pct = 0.0
    if duration >= MIN_DURATION_TICKS:
        pct = max(0.0, min(1.0, position / float(duration)))

    state = "paused" if is_paused else "playing"
    return PlaybackSnapshot(state=state, pct=pct, item_id=item_id, title=pretty)


class NpsController:
    """
    MOSFET low-side switch driven by PWMLED.
    - playing: solid ON
    - paused: smooth background breathe (clamped below full to avoid peak jump)
    - stopped: OFF
    """
    def __init__(self):
        self.enabled = NPS_ENABLED and NPS_AVAILABLE
        self._led = PWMLED(NPS_GPIO) if self.enabled else None
        self._mode = "stopped"
        self._t0 = time.monotonic()

        # Tune these if you like
        self._pause_min = 0.10   # never fully off during pause breathe
        self._pause_max = 0.95   # never hit full; avoids the "peak jump"

        if self.enabled:
            self._led.source = None
            self._led.off()

    def _stop_background(self):
        if not self.enabled:
            return
        self._led.source = None

    def _breathe_source(self):
        """
        Generator for gpiozero .source:
        yields brightness values in [pause_min..pause_max]
        """
        period = max(0.6, float(NPS_FADE_PERIOD_S))
        while True:
            t = time.monotonic() - self._t0
            # 0..1 sine wave
            w = (math.cos(2.0 * math.pi * (t / period)) + 1.0) * 0.5
            yield self._pause_min + (self._pause_max - self._pause_min) * w

    def set_state(self, state: str):
        if not self.enabled:    
            return

        state = state if state in ("playing", "paused") else "stopped"
        if state == self._mode:
            return  # don't restart background generation every poll

        self._mode = state
        self._stop_background()

        if state == "playing":
            self._led.on()

        elif state == "paused":
            self._t0 = time.monotonic()
            # Drive PWM from our generator (smooth, no peak jump)
            self._led.source = self._breathe_source()

        else:
            self._led.off()


def main():
    print("Jellyfin → Pico bridge starting")
    print("Jellyfin:", JELLYFIN_BASE)
    print("Target device:", JELLYFIN_DEVICE_NAME)
    print("Pico:", PICO_BASE)
    print("POLL_SECONDS:", POLL_SECONDS)
    print("PICO_STATUS_SECONDS:", PICO_STATUS_SECONDS)
    print("SESSION_GRACE_S:", SESSION_GRACE_S)
    if NPS_ENABLED:
        print("NPS:", "enabled" if NPS_AVAILABLE else "requested but gpiozero missing")

    http = make_session()
    pico = make_session()
    nps = NpsController()

    last = PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")
    last_good_snap = last
    last_seen_session_t = 0.0

    progress_mode_enabled = False
    last_status_check = 0.0
    last_jellyfin_poll = 0.0

    while True:
        now = time.monotonic()

        # 1) Refresh Pico status occasionally (don’t hammer it)
        if now - last_status_check >= PICO_STATUS_SECONDS:
            last_status_check = now
            try:
                st = pico_get_json(pico, "/api/status", timeout=4.0)
                progress_mode_enabled = bool(st.get("progress_mode_enabled", False))
            except Exception as e:
                # If Pico is unreachable sometimes, keep last known progress_mode_enabled
                print("Pico status error:", repr(e))

        # 2) Poll Jellyfin on schedule
        if now - last_jellyfin_poll < POLL_SECONDS:
            time.sleep(0.02)
            continue
        last_jellyfin_poll = now

        try:
            sessions = jellyfin_sessions(http)
            target = find_target_session(sessions, JELLYFIN_DEVICE_NAME)

            if target is not None:
                snap = snapshot_from_session(target)
                last_seen_session_t = now
                last_good_snap = snap
            else:
                # Hold last known state for a grace period (prevents flicker)
                if now - last_seen_session_t <= SESSION_GRACE_S:
                    snap = last_good_snap
                else:
                    snap = PlaybackSnapshot(state="stopped", pct=0.0, item_id=None, title="")

            # 3) Update NPS state (independent of progress mode)
            nps.set_state(snap.state if snap.state in ("playing", "paused") else "stopped")

            # 4) If progress mode off, don’t drive Pico visuals
            if not progress_mode_enabled:
                last = snap
                continue

            # 5) Progress mode on: translate to Pico
            item_changed = (snap.item_id is not None and snap.item_id != last.item_id)

            if snap.state == "playing":
                if last.state != "playing" or item_changed:
                    pico_call(pico, "/api/event?name=movie_start", timeout=4.0)
                pico_call(pico, f"/api/progress?pct={snap.pct:.6f}&state=playing", timeout=4.0)

            elif snap.state == "paused":
                if last.state != "paused":
                    pico_call(pico, "/api/event?name=movie_pause", timeout=4.0)
                pico_call(pico, f"/api/progress?pct={snap.pct:.6f}&state=paused", timeout=4.0)

            else:
                if last.state != "stopped":
                    pico_call(pico, "/api/event?name=movie_stop", timeout=4.0)
                pico_call(pico, "/api/progress?state=stopped", timeout=4.0)

            if snap.state != last.state or item_changed:
                print(f"[{time.strftime('%H:%M:%S')}] {snap.state.upper()} pct={snap.pct:.3f} title={snap.title}")

            last = snap

        except Exception as e:
            # Keep running forever; session grace prevents NPS flicker on brief issues
            print("Bridge error:", repr(e))


if __name__ == "__main__":
    main()
