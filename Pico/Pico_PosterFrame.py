# Poster / Marquee lights - Step 4
# - Tick-based, non-blocking effects
# - SPA-style web UI (fetch; no navigation)
# - JSON API under /api/*
# - /api/event for semantic triggers (Pico chooses show)
# - /api/progress for Jellyfin-style progress bar mode (push from a Pi)
#
# Raspberry Pi Pico W (MicroPython)

import network
import asyncio
import time
import random
import math
from machine import Pin
import neopixel
import config

# -------------------------------
# Wi-Fi
# -------------------------------
SSID = config.REMOTE_SSID
PASSWORD = config.REMOTE_PASSWORD


def init_wifi(ssid: str, password: str) -> bool:
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(ssid, password)

    timeout = 15
    while timeout > 0:
        status = wlan.status()
        if status >= 3:
            break
        timeout -= 1
        time.sleep(1)

    if wlan.status() != 3:
        print("Wi-Fi connect failed, status:", wlan.status())
        return False

    print("Wi-Fi connected, IP:", wlan.ifconfig()[0])
    return True


# -------------------------------
# NeoPixel configuration
# -------------------------------
NEOPIXEL_PIN = 28
NEOPIXEL_COUNT = 20  # change to 40/60 if you add strips later

np = neopixel.NeoPixel(Pin(NEOPIXEL_PIN, Pin.OUT), NEOPIXEL_COUNT)

neopixel_enabled = True

# User-adjustable globals (via /api/config)
brightness = 0.60     # 0..1
speed = 1.00          # 0.2..3.0 (multiplier, higher is faster)

def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x

# Your strip expects (G, B, R)
def neo_color_rgb(r: int, g: int, b: int) -> tuple:
    s = clamp(float(brightness), 0.0, 1.0)
    r = int(clamp(r * s, 0, 255))
    g = int(clamp(g * s, 0, 255))
    b = int(clamp(b * s, 0, 255))
    return (g, b, r)

def clear_np():
    for i in range(NEOPIXEL_COUNT):
        np[i] = (0, 0, 0)
    np.write()

# Colors in normal RGB; converted via neo_color_rgb()
WARM = (255, 140, 20)
SOFT_WARM = (255, 100, 10)
GOLD = (255, 180, 30)
COOL = (50, 120, 255)  # used for progress head highlight if desired

def scale_rgb(rgb, s: float):
    s = clamp(float(s), 0.0, 1.0)
    r, g, b = rgb
    return (int(r * s), int(g * s), int(b * s))


# -------------------------------
# Config (no magic numbers live in code anymore)
# -------------------------------
EFFECT_CONFIG = {
    "twinkle": {
        "color": SOFT_WARM,
        "base_min": 0.25,
        "base_max": 0.55,
        "twinkle_chance": 0.25,
        "twinkle_boost_min": 0.35,
        "twinkle_boost_max": 0.80,
        "twinkle_decay": 0.82,
        "frame_ms": 60,
    },

    "breath": {
        "color": WARM,
        "min_s": 0.06,
        "max_s": 0.70,
        "frame_ms": 20,
        "phase_step": 0.06,
    },

    "marquee": {
        "color": WARM,
        "bulb_every": 3,
        "duty": 0.90,
        "base_dim": 0.03,
        "step_ms": 110,
    },

    "wipe_show": {
        "color": WARM,
        "pop_color": GOLD,
        "hold_s": 0.9,
        "pop_rate": 0.22,
        "step_ms": 20,
    },

    "double_show": {
        "color": WARM,
        "tail": 4,
        "bg": 0.03,
        "step_ms": 50,
    },

    "progress": {
        "filled_color": WARM,
        "empty_dim": 0.04,          # unfilled segment brightness
        "filled_dim": 0.70,         # filled segment brightness
        "head_boost": 0.25,         # extra brightness at the head
        "frame_ms": 50,
        "timeout_ms": 30000,        # if no updates in 30s, revert to idle
        "paused_blink_ms": 500,     # head blink rate when paused
    },
}

# -------------------------------
# URL parsing helpers
# -------------------------------
def parse_path_and_query(path: str):
    if "?" not in path:
        return path, {}
    route, qs = path.split("?", 1)
    params = {}
    for part in qs.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, "1"
        params[k] = v
    return route, params

def as_float(params, key, default):
    try:
        return float(params.get(key, default))
    except:
        return default

def as_int(params, key, default):
    try:
        return int(params.get(key, default))
    except:
        return default

def as_str(params, key, default=""):
    try:
        return str(params.get(key, default))
    except:
        return default

# -------------------------------
# Tick-based effects
# -------------------------------
class Effect:
    def reset(self):
        pass

    def tick(self, now_ms: int):
        pass

def scaled_ms(base_ms: int) -> int:
    sp = clamp(float(speed), 0.2, 3.0)
    return int(max(5, base_ms / sp))


class BreathingGlow(Effect):
    def __init__(self, cfg):
        self.color = cfg["color"]
        self.min_s = cfg["min_s"]
        self.max_s = cfg["max_s"]
        self.base_frame_ms = cfg["frame_ms"]
        self.phase_step = cfg["phase_step"]
        self._phase = 0.0
        self._next_ms = 0

    def reset(self):
        self._phase = 0.0
        self._next_ms = 0

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return

        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.base_frame_ms))

        w = (math.sin(self._phase) + 1.0) * 0.5
        s = self.min_s + (self.max_s - self.min_s) * w
        r, g, b = scale_rgb(self.color, s)
        c = neo_color_rgb(r, g, b)

        for i in range(NEOPIXEL_COUNT):
            np[i] = c
        np.write()

        self._phase += self.phase_step


class MarqueeChase(Effect):
    def __init__(self, cfg):
        self.color = cfg["color"]
        self.bulb_every = max(1, int(cfg["bulb_every"]))
        self.duty = cfg["duty"]
        self.base_dim = cfg["base_dim"]
        self.base_step_ms = cfg["step_ms"]
        self._offset = 0
        self._next_ms = 0

    def reset(self):
        self._offset = 0
        self._next_ms = 0

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return

        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.base_step_ms))

        for i in range(NEOPIXEL_COUNT):
            on = ((i + self._offset) % self.bulb_every) == 0
            s = self.duty if on else self.base_dim
            r, g, b = scale_rgb(self.color, s)
            np[i] = neo_color_rgb(r, g, b)

        np.write()
        self._offset = (self._offset + 1) % self.bulb_every


class BulbTwinkle(Effect):
    """Obvious but classy marquee-style twinkle."""
    def __init__(self, cfg):
        self.color = cfg["color"]
        self.base_min = cfg["base_min"]
        self.base_max = cfg["base_max"]
        self.twinkle_chance = cfg["twinkle_chance"]
        self.twinkle_boost_min = cfg["twinkle_boost_min"]
        self.twinkle_boost_max = cfg["twinkle_boost_max"]
        self.twinkle_decay = cfg["twinkle_decay"]
        self.base_frame_ms = cfg["frame_ms"]

        self._base = [0.0] * NEOPIXEL_COUNT
        self._twinkle = [0.0] * NEOPIXEL_COUNT
        self._next_ms = 0

    def reset(self):
        for i in range(NEOPIXEL_COUNT):
            self._base[i] = self.base_min + random.random() * (self.base_max - self.base_min)
            self._twinkle[i] = 0.0
        self._next_ms = 0

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return

        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.base_frame_ms))

        if random.random() < self.twinkle_chance:
            j = random.randrange(NEOPIXEL_COUNT)
            boost = self.twinkle_boost_min + random.random() * (self.twinkle_boost_max - self.twinkle_boost_min)
            if boost > self._twinkle[j]:
                self._twinkle[j] = boost

        for i in range(NEOPIXEL_COUNT):
            self._base[i] += (random.random() - 0.5) * 0.03
            if self._base[i] < self.base_min:
                self._base[i] = self.base_min
            if self._base[i] > self.base_max:
                self._base[i] = self.base_max

            self._twinkle[i] *= self.twinkle_decay
            if self._twinkle[i] < 0.01:
                self._twinkle[i] = 0.0

            level = self._base[i] + self._twinkle[i]
            if level > 1.0:
                level = 1.0

            r, g, b = scale_rgb(self.color, level)
            np[i] = neo_color_rgb(r, g, b)

        np.write()


# --- Show effects (overlay) ---
class DoubleChaseShow(Effect):
    def __init__(self, cfg):
        self.color = cfg["color"]
        self.tail = max(0, int(cfg["tail"]))
        self.base_step_ms = cfg["step_ms"]
        self.bg = cfg["bg"]
        self._a = 0
        self._b = NEOPIXEL_COUNT - 1
        self._next_ms = 0

    def reset(self):
        self._a = 0
        self._b = NEOPIXEL_COUNT - 1
        self._next_ms = 0

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return
        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.base_step_ms))

        br, bg, bb = scale_rgb(self.color, self.bg)
        bgc = neo_color_rgb(br, bg, bb)
        for i in range(NEOPIXEL_COUNT):
            np[i] = bgc

        for t in range(self.tail + 1):
            i = (self._a - t) % NEOPIXEL_COUNT
            s = 1.0 - (t / (self.tail + 1)) if self.tail >= 0 else 1.0
            r, g, b = scale_rgb(self.color, 0.95 * s)
            np[i] = neo_color_rgb(r, g, b)

        for t in range(self.tail + 1):
            i = (self._b + t) % NEOPIXEL_COUNT
            s = 1.0 - (t / (self.tail + 1)) if self.tail >= 0 else 1.0
            r, g, b = scale_rgb(self.color, 0.95 * s)
            np[i] = neo_color_rgb(r, g, b)

        np.write()
        self._a = (self._a + 1) % NEOPIXEL_COUNT
        self._b = (self._b - 1) % NEOPIXEL_COUNT


class WipeHoldPopShow(Effect):
    # Loops until show time ends: wipe on -> hold+pops -> wipe off -> repeat
    def __init__(self, cfg):
        self.color = cfg["color"]
        self.pop_color = cfg["pop_color"]
        self.hold_s = cfg["hold_s"]
        self.pop_rate = cfg["pop_rate"]
        self.base_step_ms = cfg["step_ms"]

        self._stage = 0  # 0 wipe on, 1 hold, 2 wipe off
        self._idx = 0
        self._hold_until = 0
        self._next_ms = 0

    def reset(self):
        self._stage = 0
        self._idx = 0
        self._hold_until = 0
        self._next_ms = 0
        clear_np()

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return
        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.base_step_ms))

        main_c = neo_color_rgb(*scale_rgb(self.color, 0.85))
        pop_c = neo_color_rgb(*scale_rgb(self.pop_color, 1.0))

        if self._stage == 0:
            if self._idx < NEOPIXEL_COUNT:
                np[self._idx] = main_c
                np.write()
                self._idx += 1
            else:
                self._stage = 1
                self._hold_until = time.ticks_add(now_ms, int(self.hold_s * 1000))

        elif self._stage == 1:
            for i in range(NEOPIXEL_COUNT):
                np[i] = main_c
            if random.random() < self.pop_rate:
                j = random.randrange(NEOPIXEL_COUNT)
                np[j] = pop_c
            np.write()

            if time.ticks_diff(now_ms, self._hold_until) >= 0:
                self._stage = 2
                self._idx = 0

        else:
            if self._idx < NEOPIXEL_COUNT:
                np[self._idx] = (0, 0, 0)
                np.write()
                self._idx += 1
            else:
                self._stage = 0
                self._idx = 0


# --- Progress bar effect (push-driven) ---
class ProgressBar(Effect):
    """
    Renders a progress bar around the strip.
    Use via /api/progress?pct=0.0..1.0&state=playing|paused|stopped

    When active (recent updates), it overrides idle (but NOT a show overlay).
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self._next_ms = 0
        self.pct = 0.0
        self.state = "stopped"
        self._last_update_ms = 0
        self._blink_on = True
        self._next_blink_ms = 0
        self._phase = 0.0

    def reset(self):
        self._next_ms = 0
        self._last_update_ms = 0
        self._blink_on = True
        self._next_blink_ms = 0
        self._phase = 0.0

    def update(self, now_ms: int, pct: float, state: str):
        self.pct = clamp(pct, 0.0, 1.0)
        self.state = state if state in ("playing", "paused", "stopped") else "playing"
        self._last_update_ms = now_ms

    def active(self, now_ms: int) -> bool:
        return time.ticks_diff(now_ms, time.ticks_add(self._last_update_ms, self.cfg["timeout_ms"])) < 0

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return

        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.cfg["frame_ms"]))

        filled_color = self.cfg["filled_color"]
        empty_dim = self.cfg["empty_dim"]
        filled_dim = self.cfg["filled_dim"]
        head_boost = self.cfg["head_boost"]

        # compute how many pixels are "filled"
        n = NEOPIXEL_COUNT
        filled = int(self.pct * n + 0.0001)
        if filled > n:
            filled = n

        # head pixel index (where progress currently is)
        head = filled if filled < n else n - 1

        # paused blink logic (blink the head)
        if self.state == "paused":
            if self._next_blink_ms == 0:
                self._next_blink_ms = time.ticks_add(now_ms, self.cfg["paused_blink_ms"])
            if time.ticks_diff(now_ms, self._next_blink_ms) >= 0:
                self._blink_on = not self._blink_on
                self._next_blink_ms = time.ticks_add(now_ms, self.cfg["paused_blink_ms"])
        else:
            self._blink_on = True
            self._next_blink_ms = 0

        # subtle "life" in filled area (very gentle pulse)
        self._phase += 0.05
        pulse = 0.05 * (math.sin(self._phase) + 1.0) * 0.5  # 0..~0.05

        for i in range(n):
            if i < filled:
                s = clamp(filled_dim + pulse, 0.0, 1.0)
                r, g, b = scale_rgb(filled_color, s)
                np[i] = neo_color_rgb(r, g, b)
            else:
                r, g, b = scale_rgb(filled_color, empty_dim)
                np[i] = neo_color_rgb(r, g, b)

        # head highlight
        if n > 0 and self._blink_on and self.state != "stopped":
            base = filled_dim
            r, g, b = scale_rgb(filled_color, clamp(base + head_boost, 0.0, 1.0))
            np[head] = neo_color_rgb(r, g, b)

        np.write()


# -------------------------------
# Registries / state
# -------------------------------
IDLE_EFFECTS = {
    "twinkle": BulbTwinkle(EFFECT_CONFIG["twinkle"]),
    "breath": BreathingGlow(EFFECT_CONFIG["breath"]),
}

SHOW_EFFECTS = {
    "wipe": WipeHoldPopShow(EFFECT_CONFIG["wipe_show"]),
    "double": DoubleChaseShow(EFFECT_CONFIG["double_show"]),
    "marquee": MarqueeChase(EFFECT_CONFIG["marquee"]),
}

# Default idle
idle_name = "twinkle"
idle_effect = IDLE_EFFECTS[idle_name]
idle_effect.reset()

# Show overlay state
show_active = False
show_name = ""
show_effect = None
show_until_ms = 0

# Demo mode (cycles idle effects only, never interrupts show)
demo_mode = False
demo_interval_s = 15
_last_demo_switch_ms = 0
_demo_order = ["twinkle", "breath"]
_demo_idx = 0

# Progress mode (push updates); overrides idle when active; does NOT interrupt shows
progress_effect = ProgressBar(EFFECT_CONFIG["progress"])
progress_effect.reset()

# Event mapping: send semantic events, Pico picks show/duration
# (You can change these without touching your Pi)
EVENT_MAP = {
    # event_name: {"shows": [list], "seconds": default_duration, "mode": optional idle change}
    "bulb_change": {"shows": ["wipe", "double", "marquee"], "seconds": 8},
    "movie_start":  {"shows": ["wipe"], "seconds": 10},
    "movie_pause":  {"shows": ["double"], "seconds": 6},
    "movie_stop":   {"shows": ["wipe"], "seconds": 6},
}

# To avoid repeating the same show every time, we rotate within each event
_event_rotator = {k: 0 for k in EVENT_MAP.keys()}


def set_idle(name: str) -> bool:
    global idle_name, idle_effect
    if name not in IDLE_EFFECTS:
        return False
    idle_name = name
    idle_effect = IDLE_EFFECTS[name]
    idle_effect.reset()
    return True


def start_show(name: str, seconds: int) -> bool:
    global show_active, show_name, show_effect, show_until_ms
    if name not in SHOW_EFFECTS:
        return False
    seconds = int(clamp(seconds, 1, 60))
    show_name = name
    show_effect = SHOW_EFFECTS[name]
    show_effect.reset()
    show_active = True
    show_until_ms = time.ticks_add(time.ticks_ms(), seconds * 1000)
    return True


def stop_show():
    global show_active, show_name, show_effect, show_until_ms
    show_active = False
    show_name = ""
    show_effect = None
    show_until_ms = 0


def trigger_event(event_name: str, seconds_override: int = None) -> dict:
    """
    Returns dict with result info: {"ok": bool, "event":..., "show":..., "seconds":..., "message":...}
    """
    if event_name not in EVENT_MAP:
        return {"ok": False, "event": event_name, "message": "Unknown event"}

    cfg = EVENT_MAP[event_name]
    shows = cfg.get("shows", [])
    if not shows:
        return {"ok": False, "event": event_name, "message": "No shows configured for event"}

    # rotate show choice per event (predictable)
    idx = _event_rotator.get(event_name, 0) % len(shows)
    _event_rotator[event_name] = idx + 1
    chosen = shows[idx]

    seconds = cfg.get("seconds", 8)
    if seconds_override is not None:
        seconds = int(clamp(seconds_override, 1, 60))

    ok = start_show(chosen, seconds)
    return {
        "ok": ok,
        "event": event_name,
        "show": chosen,
        "seconds": seconds,
        "message": "OK" if ok else "Failed to start show",
    }


def status_dict():
    now = time.ticks_ms()
    return {
        "enabled": neopixel_enabled,
        "count": NEOPIXEL_COUNT,
        "brightness": brightness,
        "speed": speed,
        "idle": idle_name,
        "show_active": show_active,
        "show": show_name,
        "show_ms_remaining": max(0, time.ticks_diff(show_until_ms, now)) if show_active else 0,
        "demo": demo_mode,
        "demo_interval_s": demo_interval_s,
        "progress_active": progress_effect.active(now),
        "progress_pct": progress_effect.pct,
        "progress_state": progress_effect.state,
        
        # NEW: advertise capabilities for dynamic UI
        "idle_modes": list(IDLE_EFFECTS.keys()),
        "show_modes": list(SHOW_EFFECTS.keys()),
        "events": list(EVENT_MAP.keys()),
    }


# -------------------------------
# Minimal JSON encoder (enough for our simple dicts)
# -------------------------------
def json_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")

def to_json(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        # keep it simple
        return str(v)
    if isinstance(v, str):
        return '"' + json_escape(v) + '"'
    if isinstance(v, dict):
        items = []
        for k in v:
            items.append(to_json(str(k)) + ":" + to_json(v[k]))
        return "{" + ",".join(items) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(to_json(x) for x in v) + "]"
    return to_json(str(v))


# -------------------------------
# Engine task
# -------------------------------
async def neopixel_engine_task():
    global _last_demo_switch_ms, _demo_idx

    while True:
        now = time.ticks_ms()

        if not neopixel_enabled:
            await asyncio.sleep_ms(50)
            continue

        # demo: cycles idle effects, but never interrupts a show
        if demo_mode and (not show_active):
            if _last_demo_switch_ms == 0:
                _last_demo_switch_ms = now
            if time.ticks_diff(now, _last_demo_switch_ms) >= int(demo_interval_s * 1000):
                _demo_idx = (_demo_idx + 1) % len(_demo_order)
                set_idle(_demo_order[_demo_idx])
                _last_demo_switch_ms = now

        # show overlay wins
        if show_active and show_effect is not None:
            show_effect.tick(now)
            if time.ticks_diff(now, show_until_ms) >= 0:
                stop_show()

        else:
            # progress overrides idle if active
            if progress_effect.active(now) and progress_effect.state != "stopped":
                progress_effect.tick(now)
            else:
                idle_effect.tick(now)

        await asyncio.sleep_ms(10)


# -------------------------------
# Web UI (single page; JS fetches /api/*)
# -------------------------------
def html_page():
    timeout_s = int(EFFECT_CONFIG["progress"]["timeout_ms"] / 1000)
    return f"""\
HTTP/1.0 200 OK\r
Content-Type: text/html\r
Connection: close\r
\r
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Poster Lights</title>
  <style>
    body {{ font-family: sans-serif; margin: 16px; }}
    .row {{ margin: 10px 0; display: flex; flex-wrap: wrap; gap: 8px; }}
    button {{ padding: 12px 14px; }}
    input, select {{ padding: 10px; }}
    .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 12px; margin: 10px 0; }}
    pre {{ white-space: pre-wrap; margin: 0; }}
    .small {{ color:#666; font-size: 0.92em; }}
    .ok {{ color: #0a7; }}
    .err {{ color: #b00; }}
    code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h2>Poster Lights</h2>

  <div class="card">
    <b>Status</b>
    <div class="row">
      <button onclick="refreshStatus()">Refresh now</button>
      <button onclick="api('/api/np_on')">NeoPixels ON</button>
      <button onclick="api('/api/np_off')">NeoPixels OFF</button>
    </div>
    <pre id="status">(loading...)</pre>
    <div id="msg" class="small"></div>
  </div>

  <div class="card">
    <b>Idle Mode (runs continuously)</b>
    <div id="idleButtons" class="row"></div>
  </div>

  <div class="card">
    <b>Show (temporary overlay)</b>
    <div id="showButtons" class="row"></div>

    <div class="small">Or trigger semantic events:</div>
    <div id="eventButtons" class="row"></div>
  </div>

  <div class="card">
    <b>Demo Mode</b>
    <div class="row">
      <button onclick="api('/api/demo?on=1&interval=15')">Demo ON (15s)</button>
      <button onclick="api('/api/demo?on=0')">Demo OFF</button>
    </div>
  </div>

  <div class="card">
    <b>Config</b>
    <div class="row">
      <label>Brightness (0..1)
        <input id="brightness" value="{brightness}">
      </label>
      <label>Speed (0.2..3.0)
        <input id="speed" value="{speed}">
      </label>
      <button onclick="applyConfig()">Apply</button>
    </div>
    <div class="small">Tip: your Pi can call <code>/api/event?name=bulb_change</code> or <code>/api/progress?pct=0.42&state=playing</code></div>
  </div>

  <div class="card">
    <b>Progress Bar (test)</b>
    <div class="row">
      <button onclick="api('/api/progress?pct=0.10&state=playing')">10%</button>
      <button onclick="api('/api/progress?pct=0.50&state=playing')">50%</button>
      <button onclick="api('/api/progress?pct=0.90&state=playing')">90%</button>
      <button onclick="api('/api/progress?pct=0.90&state=paused')">Paused @ 90%</button>
      <button onclick="api('/api/progress?state=stopped')">Stop progress mode</button>
    </div>
    <div class="small">If no progress updates arrive for ~{timeout_s}s, it returns to idle automatically.</div>
  </div>

<script>
async function api(url) {{
  try {{
    const res = await fetch(url);
    const txt = await res.text();
    let data = null;
    try {{ data = JSON.parse(txt); }} catch (e) {{}}
    if (data && data.ok === false) {{
      setMsg("✗ " + (data.message || "Error"), true);
    }} else {{
      setMsg("✓ OK", false);
    }}
  }} catch (e) {{
    setMsg("✗ " + e, true);
  }}
  refreshStatus();
}}

function setMsg(msg, isErr) {{
  const el = document.getElementById("msg");
  el.className = "small " + (isErr ? "err" : "ok");
  el.textContent = msg;
}}

function makeButton(label, onclickFn) {{
  const b = document.createElement("button");
  b.textContent = label;
  b.onclick = onclickFn;
  return b;
}}

function renderButtons(data) {{
  const idleWrap = document.getElementById("idleButtons");
  const showWrap = document.getElementById("showButtons");
  const eventWrap = document.getElementById("eventButtons");

  idleWrap.innerHTML = "";
  showWrap.innerHTML = "";
  eventWrap.innerHTML = "";

  (data.idle_modes || []).forEach(function(name) {{
    idleWrap.appendChild(
      makeButton(name, function() {{
        api("/api/mode?name=" + encodeURIComponent(name));
      }})
    );
  }});

  (data.show_modes || []).forEach(function(name) {{
    const seconds = (name === "wipe") ? 8 : (name === "double") ? 10 : 10;
    showWrap.appendChild(
      makeButton(name + " (" + seconds + "s)", function() {{
        api("/api/show?name=" + encodeURIComponent(name) + "&seconds=" + seconds);
      }})
    );
  }});

  (data.events || []).forEach(function(name) {{
    eventWrap.appendChild(
      makeButton("Event: " + name, function() {{
        api("/api/event?name=" + encodeURIComponent(name));
      }})
    );
  }});
}}

async function refreshStatus() {{
  try {{
    const res = await fetch("/api/status");
    const data = await res.json();
    document.getElementById("status").textContent = JSON.stringify(data, null, 2);
    document.getElementById("brightness").value = data.brightness;
    document.getElementById("speed").value = data.speed;
    renderButtons(data);
  }} catch (e) {{
    document.getElementById("status").textContent = "(status fetch failed) " + e;
  }}
}}

function applyConfig() {{
  const b = encodeURIComponent(document.getElementById("brightness").value);
  const s = encodeURIComponent(document.getElementById("speed").value);
  api("/api/config?brightness=" + b + "&speed=" + s);
}}

refreshStatus();
setInterval(refreshStatus, 1200);
</script>

</body>
</html>
"""

# -------------------------------
# HTTP response helpers
# -------------------------------
async def respond_text(writer, body: str, code="200 OK", content_type="text/plain"):
    resp = (
        "HTTP/1.0 " + code + "\r\n"
        "Content-Type: " + content_type + "\r\n"
        "Connection: close\r\n\r\n"
        + body
    )
    await writer.awrite(resp)
    await writer.aclose()

async def respond_json(writer, obj, code="200 OK"):
    await respond_text(writer, to_json(obj), code=code, content_type="application/json")


# -------------------------------
# Request handler
# -------------------------------
async def handle_client(reader, writer):
    global neopixel_enabled, brightness, speed, demo_mode, demo_interval_s, _last_demo_switch_ms, _demo_idx

    try:
        request_line = await reader.readline()
        if not request_line:
            await writer.aclose()
            return

        parts = request_line.decode().split()
        path = parts[1] if len(parts) >= 2 else "/"

        # drain headers
        while True:
            line = await reader.readline()
            if not line or line == b"\r\n":
                break

        route, params = parse_path_and_query(path)

        # UI
        if route == "/" or route == "/index.html":
            await writer.awrite(html_page())
            await writer.aclose()
            return

        # JSON API
        if route.startswith("/api/"):
            now = time.ticks_ms()

            if route == "/api/status":
                await respond_json(writer, status_dict())
                return

            if route == "/api/np_on":
                neopixel_enabled = True
                await respond_json(writer, {"ok": True, "message": "NeoPixels enabled"})
                return

            if route == "/api/np_off":
                neopixel_enabled = False
                clear_np()
                await respond_json(writer, {"ok": True, "message": "NeoPixels disabled"})
                return

            if route == "/api/mode":
                name = as_str(params, "name", "")
                ok = set_idle(name)
                await respond_json(writer, {"ok": ok, "message": "OK" if ok else "Unknown idle mode", "idle": idle_name})
                return

            if route == "/api/show":
                name = as_str(params, "name", "")
                seconds = as_int(params, "seconds", 8)
                ok = start_show(name, seconds)
                await respond_json(writer, {"ok": ok, "message": "OK" if ok else "Unknown show", "show": show_name, "seconds": seconds})
                return

            if route == "/api/demo":
                on = as_str(params, "on", "0").lower()
                if on in ("1", "true", "yes", "on"):
                    demo_mode = True
                    demo_interval_s = as_int(params, "interval", demo_interval_s)
                    demo_interval_s = int(clamp(demo_interval_s, 5, 120))
                    if idle_name in _demo_order:
                        _demo_idx = _demo_order.index(idle_name)
                    _last_demo_switch_ms = now
                    await respond_json(writer, {"ok": True, "message": "Demo enabled", "interval": demo_interval_s})
                else:
                    demo_mode = False
                    await respond_json(writer, {"ok": True, "message": "Demo disabled"})
                return

            if route == "/api/config":
                b = as_float(params, "brightness", brightness)
                s = as_float(params, "speed", speed)
                brightness = clamp(b, 0.0, 1.0)
                speed = clamp(s, 0.2, 3.0)
                await respond_json(writer, {"ok": True, "message": "Config applied", "brightness": brightness, "speed": speed})
                return

            if route == "/api/event":
                name = as_str(params, "name", "")
                seconds_override = params.get("seconds", None)
                if seconds_override is not None:
                    seconds_override = as_int(params, "seconds", 8)
                result = trigger_event(name, seconds_override=seconds_override)
                await respond_json(writer, result)
                return

            if route == "/api/progress":
                state = as_str(params, "state", "playing").lower()
                if state == "stopped":
                    progress_effect.update(now, 0.0, "stopped")
                    await respond_json(writer, {"ok": True, "message": "Progress stopped"})
                    return

                pct = as_float(params, "pct", progress_effect.pct)
                progress_effect.update(now, pct, state)
                await respond_json(writer, {"ok": True, "message": "Progress updated", "pct": progress_effect.pct, "state": progress_effect.state})
                return

            # unknown api
            await respond_json(writer, {"ok": False, "message": "Unknown API route"}, code="404 Not Found")
            return

        # Non-api fallback
        await respond_text(writer, "Not found\n", code="404 Not Found")
        return

    except Exception as e:
        try:
            await writer.aclose()
        except:
            pass
        print("Client error:", e)


# -------------------------------
# Main
# -------------------------------
async def main():
    if not init_wifi(SSID, PASSWORD):
        return

    clear_np()

    asyncio.create_task(neopixel_engine_task())

    server = asyncio.start_server(handle_client, "0.0.0.0", 80)
    asyncio.create_task(server)
    print("HTTP server running on port 80")

    while True:
        await asyncio.sleep(5)


loop = asyncio.get_event_loop()
loop.create_task(main())
loop.run_forever()
