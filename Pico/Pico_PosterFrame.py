# Poster / Marquee lights - Step 5
# - Adds Progress Mode (enable/disable)
# - Progress uses configurable arc (default start=1 end=18) with dim warm trim outside arc
# - Playing: fill-with-fade + twinkle on filled pixels
# - Paused: two opposite orbiting bulbs
# - When progress mode is enabled AND progress is active, show triggers are ignored
# - /api/status advertises show defaults (UI no longer hardcodes durations)

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

# Static IP configuration
static_ip = '192.168.0.250'  # Replace with your desired static IP
subnet_mask = '255.255.255.0'
gateway_ip = '192.168.0.255'
dns_server = '8.8.8.8'

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
        
    # Set static IP address
    wlan.ifconfig((static_ip, subnet_mask, gateway_ip, dns_server))

    if wlan.status() != 3:
        print("Wi-Fi connect failed, status:", wlan.status())
        return False

    print("Wi-Fi connected, IP:", wlan.ifconfig()[0])
    return True

# -------------------------------
# NeoPixel configuration
# -------------------------------
NEOPIXEL_PIN = 28
NEOPIXEL_COUNT = 20

np = neopixel.NeoPixel(Pin(NEOPIXEL_PIN, Pin.OUT), NEOPIXEL_COUNT)

neopixel_enabled = True

# User-adjustable globals (via /api/config)
brightness = 0.60     # 0..1
speed = 1.00          # 0.2..3.0

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

# Colors in normal RGB
WARM = (255, 140, 20)
SOFT_WARM = (255, 100, 10)
GOLD = (255, 180, 30)
RED = (255, 0, 0)
WHITE = (255, 255, 255)

def scale_rgb(rgb, s: float):
    s = clamp(float(s), 0.0, 1.0)
    r, g, b = rgb
    return (int(r * s), int(g * s), int(b * s))

# -------------------------------
# Config
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
        "empty_dim": 0.04,          # within arc, ahead of head
        "filled_dim": 0.70,         # base brightness for filled pixels
        "head_dim": 0.85,           # max brightness for active head pixel
        "frame_ms": 50,
        "timeout_ms": 30000,
        "trim_dim": 0.10,           # pixels outside arc (dim warm trim)
        "twinkle_strength": 0.22,   # how much twinkle modulates filled pixels
        "pause_step_ms": 170,       # orbit speed
        "pause_dim": 0.85,          # brightness of orbit bulbs
    },
}

# Defaults: use pixels 1..18 (zero-based) for progress
progress_start = 1
progress_end = 18

# Progress mode toggle (OFF by default)
progress_mode_enabled = False

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

# --- Shows ---
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
    # loops until show time ends
    def __init__(self, cfg):
        self.color = cfg["color"]
        self.pop_color = cfg["pop_color"]
        self.hold_s = cfg["hold_s"]
        self.pop_rate = cfg["pop_rate"]
        self.base_step_ms = cfg["step_ms"]
        self._stage = 0
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

class MovieWipeOnce(Effect):
    """
    Single-run wipe (alternating colors) across the progress arc.
    Intended for movie_start/movie_stop.
    """
    def __init__(self, colors, direction=1, step_ms=30):
        self.colors = colors
        self.direction = 1 if direction >= 0 else -1
        self.step_ms = step_ms
        self._next_ms = 0
        self._pos = 0
        self._pixels = []

    def reset(self):
        self._next_ms = 0
        self._pos = 0
        self._pixels = get_progress_pixels()

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return
        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.step_ms))

        px = self._pixels
        if not px:
            return

        # dim trim outside arc
        trim_c = neo_color_rgb(*scale_rgb(WARM, EFFECT_CONFIG["progress"]["trim_dim"]))
        for i in range(NEOPIXEL_COUNT):
            np[i] = trim_c

        # render wipe up to pos
        count = min(self._pos + 1, len(px))
        for k in range(count):
            idx = px[k] if self.direction == 1 else px[len(px) - 1 - k]
            col = self.colors[k % len(self.colors)]
            np[idx] = neo_color_rgb(*scale_rgb(col, 0.90))

        np.write()
        self._pos += 1
        if self._pos >= len(px):
            # hold finished frame; engine timer will end show
            self._pos = len(px) - 1

# -------------------------------
# Progress helpers + Progress effect (v2)
# -------------------------------
def normalize_index(i):
    # allow negatives etc
    return int(i) % NEOPIXEL_COUNT

def get_progress_pixels():
    """
    Returns list of pixel indices from progress_start..progress_end inclusive,
    wrapping if end < start.
    """
    if NEOPIXEL_COUNT <= 0:
        return []
    s = normalize_index(progress_start)
    e = normalize_index(progress_end)
    out = [s]
    while out[-1] != e:
        out.append((out[-1] + 1) % NEOPIXEL_COUNT)
        if len(out) > NEOPIXEL_COUNT:
            break
    return out

class ProgressBarV2(Effect):
    """
    /api/progress?pct=0..1&state=playing|paused|stopped
    If active, and progress mode enabled, it overrides idle and ignores shows.
    """
    def __init__(self, cfg, twinkle_cfg):
        self.cfg = cfg
        self.tw_cfg = twinkle_cfg

        self._next_ms = 0
        self.pct = 0.0
        self.state = "stopped"
        self._last_update_ms = 0
        self._pause_phase = 0.0


        # twinkle state (only used for filled pixels)
        self._base = [0.0] * NEOPIXEL_COUNT
        self._tw = [0.0] * NEOPIXEL_COUNT

        # pause orbit state
        self._pause_next_ms = 0
        self._pause_pos = 0

        self._pixels = []

    def reset(self):
        self._next_ms = 0
        self._last_update_ms = 0
        self._pause_next_ms = 0
        self._pause_pos = 0
        self._pause_phase = 0.0
        self._pixels = get_progress_pixels()
        # init twinkle baselines
        for i in range(NEOPIXEL_COUNT):
            self._base[i] = self.tw_cfg["base_min"] + random.random() * (self.tw_cfg["base_max"] - self.tw_cfg["base_min"])
            self._tw[i] = 0.0

    def update(self, now_ms: int, pct: float, state: str):
        self.pct = clamp(pct, 0.0, 1.0)
        self.state = state if state in ("playing", "paused", "stopped") else "playing"
        self._last_update_ms = now_ms

    def active(self, now_ms: int) -> bool:
        return time.ticks_diff(now_ms, time.ticks_add(self._last_update_ms, self.cfg["timeout_ms"])) < 0

    def _tick_twinkle_state(self):
        # same feel as idle twinkle but slightly toned
        if random.random() < self.tw_cfg["twinkle_chance"]:
            j = random.randrange(NEOPIXEL_COUNT)
            boost = self.tw_cfg["twinkle_boost_min"] + random.random() * (self.tw_cfg["twinkle_boost_max"] - self.tw_cfg["twinkle_boost_min"])
            if boost > self._tw[j]:
                self._tw[j] = boost

        for i in range(NEOPIXEL_COUNT):
            self._base[i] += (random.random() - 0.5) * 0.03
            if self._base[i] < self.tw_cfg["base_min"]:
                self._base[i] = self.tw_cfg["base_min"]
            if self._base[i] > self.tw_cfg["base_max"]:
                self._base[i] = self.tw_cfg["base_max"]

            self._tw[i] *= self.tw_cfg["twinkle_decay"]
            if self._tw[i] < 0.01:
                self._tw[i] = 0.0

    def tick(self, now_ms: int):
        if not neopixel_enabled:
            return

        # refresh pixel arc if config changed
        if not self._pixels:
            self._pixels = get_progress_pixels()

        if self.state == "paused":
            # render paused as "breathing" on filled portion (keeps progress visible)
            if time.ticks_diff(now_ms, self._next_ms) < 0:
                return
            self._next_ms = time.ticks_add(now_ms, scaled_ms(self.cfg["frame_ms"]))

            self._pause_phase += 0.06  # pulse speed; adjust later if needed
            self._render_paused_breath()
            return

        # playing render timer
        if time.ticks_diff(now_ms, self._next_ms) < 0:
            return
        self._next_ms = time.ticks_add(now_ms, scaled_ms(self.cfg["frame_ms"]))

        if self.state == "stopped":
            return

        self._tick_twinkle_state()
        self._render_playing()

    def _render_trim(self):
        trim_c = neo_color_rgb(*scale_rgb(WARM, self.cfg["trim_dim"]))
        for i in range(NEOPIXEL_COUNT):
            np[i] = trim_c

    def _render_paused_breath(self):
        self._render_trim()
        px = self._pixels
        if not px:
            np.write()
            return

        n = len(px)
        x = clamp(self.pct, 0.0, 1.0) * n
        filled = int(x)
        frac = x - filled
        if filled < 0:
            filled = 0
            frac = 0.0
        if filled >= n:
            filled = n
            frac = 1.0

        filled_color = self.cfg["filled_color"]

        # slow pulse factor (like breath)
        w = (math.sin(self._pause_phase) + 1.0) * 0.5  # 0..1
        # pulse the filled region between ~70% and 100% of filled_dim
        base = self.cfg["filled_dim"]
        pulse_scale = 0.10 + 0.90 * w
        filled_s = clamp(base * pulse_scale, 0.0, 1.0)

        # filled pixels pulse
        for k in range(min(filled, n)):
            idx = px[k]
            r, g, b = scale_rgb(filled_color, filled_s)
            np[idx] = neo_color_rgb(r, g, b)

        # head pixel shows partial progress (steady)
        if filled < n:
            idx = px[filled]
            head_s = self.cfg["empty_dim"] + (self.cfg["head_dim"] - self.cfg["empty_dim"]) * frac
            r, g, b = scale_rgb(filled_color, clamp(head_s, 0.0, 1.0))
            np[idx] = neo_color_rgb(r, g, b)

            # unfilled arc remains dim
            empty_c = neo_color_rgb(*scale_rgb(filled_color, self.cfg["empty_dim"]))
            for k in range(filled + 1, n):
                np[px[k]] = empty_c

        np.write()

    def _render_playing(self):
        self._render_trim()
        px = self._pixels
        if not px:
            np.write()
            return

        n = len(px)
        x = clamp(self.pct, 0.0, 1.0) * n
        filled = int(x)
        frac = x - filled

        # clamp indices
        if filled < 0:
            filled = 0
            frac = 0.0
        if filled >= n:
            filled = n
            frac = 1.0

        filled_color = self.cfg["filled_color"]

        # render filled pixels with twinkle
        strength = self.cfg["twinkle_strength"]
        base_dim = self.cfg["filled_dim"]

        for k in range(min(filled, n)):
            idx = px[k]
            level = self._base[idx] + self._tw[idx]
            # normalize around twinkle's min/max into ~0..1
            tw_min = self.tw_cfg["base_min"]
            tw_max = self.tw_cfg["base_max"] + self.tw_cfg["twinkle_boost_max"]
            t = (level - tw_min) / max(0.0001, (tw_max - tw_min))
            t = clamp(t, 0.0, 1.0)
            s = clamp(base_dim + strength * (t - 0.5), 0.0, 1.0)
            r, g, b = scale_rgb(filled_color, s)
            np[idx] = neo_color_rgb(r, g, b)

        # render head pixel fade-up
        if filled < n:
            idx = px[filled]
            head_s = self.cfg["empty_dim"] + (self.cfg["head_dim"] - self.cfg["empty_dim"]) * frac
            r, g, b = scale_rgb(filled_color, clamp(head_s, 0.0, 1.0))
            np[idx] = neo_color_rgb(r, g, b)

            # pixels ahead in arc (within arc) get empty_dim
            empty_c = neo_color_rgb(*scale_rgb(filled_color, self.cfg["empty_dim"]))
            for k in range(filled + 1, n):
                np[px[k]] = empty_c
        else:
            # fully complete: all arc pixels filled (twinkle)
            pass

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
    # movie-specific wipes (single-run, timer still bounds it)
    "movie_play": MovieWipeOnce(colors=[RED, SOFT_WARM], direction=1, step_ms=28),
    "movie_stop": MovieWipeOnce(colors=[SOFT_WARM, RED], direction=-1, step_ms=28),
}

SHOW_DEFAULT_SECONDS = {
    "wipe": 8,
    "double": 10,
    "marquee": 10,
    "movie_play": 6,
    "movie_stop": 6,
}

idle_name = "twinkle"
idle_effect = IDLE_EFFECTS[idle_name]
idle_effect.reset()

show_active = False
show_name = ""
show_effect = None
show_until_ms = 0

demo_mode = False
demo_interval_s = 15
_last_demo_switch_ms = 0
_demo_order = ["twinkle", "breath"]
_demo_idx = 0

progress_effect = ProgressBarV2(EFFECT_CONFIG["progress"], EFFECT_CONFIG["twinkle"])
progress_effect.reset()

EVENT_MAP = {
    "bulb_change": {"shows": ["wipe", "double", "marquee"], "seconds": 8},
    # for Jellyfin brain to call:
    "movie_start": {"shows": ["movie_play"], "seconds": 6},
    "movie_pause": {"shows": ["double"], "seconds": 6},
    "movie_stop":  {"shows": ["movie_stop"], "seconds": 6},
}

_event_rotator = {k: 0 for k in EVENT_MAP.keys()}

def in_active_progress(now_ms: int) -> bool:
    return progress_mode_enabled and progress_effect.active(now_ms) and progress_effect.state != "stopped"

def set_idle(name: str) -> bool:
    global idle_name, idle_effect
    if name not in IDLE_EFFECTS:
        return False
    idle_name = name
    idle_effect = IDLE_EFFECTS[name]
    idle_effect.reset()
    return True

def start_show(name: str, seconds: int, now_ms: int = None) -> (bool, str):
    global show_active, show_name, show_effect, show_until_ms

    if now_ms is None:
        now_ms = time.ticks_ms()

    # Ignore shows while active progress is driving visuals
    if in_active_progress(now_ms):
        return (False, "ignored_during_progress")

    if name not in SHOW_EFFECTS:
        return (False, "unknown_show")

    seconds = int(clamp(seconds, 1, 60))
    show_name = name
    show_effect = SHOW_EFFECTS[name]
    show_effect.reset()
    show_active = True
    show_until_ms = time.ticks_add(now_ms, seconds * 1000)
    return (True, "ok")

def stop_show():
    global show_active, show_name, show_effect, show_until_ms
    show_active = False
    show_name = ""
    show_effect = None
    show_until_ms = 0

def trigger_event(event_name: str, seconds_override: int = None, now_ms: int = None) -> dict:
    if now_ms is None:
        now_ms = time.ticks_ms()

    # Ignore show-starting events while active progress is driving visuals
    if in_active_progress(now_ms):
        return {"ok": True, "event": event_name, "message": "ignored_during_progress"}

    if event_name not in EVENT_MAP:
        return {"ok": False, "event": event_name, "message": "Unknown event"}

    cfg = EVENT_MAP[event_name]
    shows = cfg.get("shows", [])
    if not shows:
        return {"ok": False, "event": event_name, "message": "No shows configured for event"}

    idx = _event_rotator.get(event_name, 0) % len(shows)
    _event_rotator[event_name] = idx + 1
    chosen = shows[idx]

    seconds = cfg.get("seconds", 8)
    if seconds_override is not None:
        seconds = int(clamp(seconds_override, 1, 60))

    ok, reason = start_show(chosen, seconds, now_ms=now_ms)
    return {
        "ok": ok,
        "event": event_name,
        "show": chosen if ok else "",
        "seconds": seconds,
        "message": "OK" if ok else reason,
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

        "progress_mode_enabled": progress_mode_enabled,
        "progress_active": progress_effect.active(now),
        "progress_pct": progress_effect.pct,
        "progress_state": progress_effect.state,
        "progress_start": progress_start,
        "progress_end": progress_end,

        "idle_modes": list(IDLE_EFFECTS.keys()),
        "show_modes": list(SHOW_EFFECTS.keys()),
        "show_defaults": SHOW_DEFAULT_SECONDS,
        "events": list(EVENT_MAP.keys()),
    }

# -------------------------------
# Minimal JSON encoder
# -------------------------------
def json_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")

def to_json(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
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

        # demo cycles idle effects only when not showing and not active progress
        if demo_mode and (not show_active) and (not in_active_progress(now)):
            if _last_demo_switch_ms == 0:
                _last_demo_switch_ms = now
            if time.ticks_diff(now, _last_demo_switch_ms) >= int(demo_interval_s * 1000):
                _demo_idx = (_demo_idx + 1) % len(_demo_order)
                set_idle(_demo_order[_demo_idx])
                _last_demo_switch_ms = now

        # If active progress is driving, it overrides everything (per your spec)
        if in_active_progress(now):
            # ensure any existing show doesn't keep running behind the scenes
            if show_active:
                stop_show()
            progress_effect.tick(now)
            await asyncio.sleep_ms(10)
            continue

        # otherwise: shows then idle/progress fallback (progress only if enabled? no: still allowed)
        if show_active and show_effect is not None:
            show_effect.tick(now)
            if time.ticks_diff(now, show_until_ms) >= 0:
                stop_show()
        else:
            # if progress mode is enabled but not active (timed out), just idle
            idle_effect.tick(now)

        await asyncio.sleep_ms(10)

# -------------------------------
# Web UI
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
    input {{ padding: 10px; }}
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
    <b>Progress Mode</b>
    <div class="row">
      <button onclick="api('/api/progress_mode?on=1')">Progress Mode ON</button>
      <button onclick="api('/api/progress_mode?on=0')">Progress Mode OFF</button>
    </div>
    <div class="small">
      When ON and Jellyfin updates are active, show triggers are ignored. If updates stop for ~{timeout_s}s, it falls back to idle.
    </div>
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
  </div>

  <div class="card">
    <b>Progress (test)</b>
    <div class="row">
      <button onclick="api('/api/progress?pct=0.10&state=playing')">10%</button>
      <button onclick="api('/api/progress?pct=0.50&state=playing')">50%</button>
      <button onclick="api('/api/progress?pct=0.90&state=playing')">90%</button>
      <button onclick="api('/api/progress?pct=0.90&state=paused')">Paused @ 90%</button>
      <button onclick="api('/api/progress?state=stopped')">Stop progress</button>
    </div>
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

  const defaults = data.show_defaults || {{}};
  (data.show_modes || []).forEach(function(name) {{
    const seconds = defaults[name] || 10;
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

function safeSetInput(id, newValue) {{
  const el = document.getElementById(id);
  if (!el) return;
  // Don't overwrite while the user is typing
  if (document.activeElement === el) return;
  el.value = newValue;
}}

async function refreshStatus() {{
  try {{
    const res = await fetch("/api/status");
    const data = await res.json();
    document.getElementById("status").textContent = JSON.stringify(data, null, 2);

    safeSetInput("brightness", data.brightness);
    safeSetInput("speed", data.speed);

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
    global progress_mode_enabled, progress_start, progress_end

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

        if route == "/" or route == "/index.html":
            await writer.awrite(html_page())
            await writer.aclose()
            return

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
                seconds = as_int(params, "seconds", SHOW_DEFAULT_SECONDS.get(name, 10))
                ok, reason = start_show(name, seconds, now_ms=now)
                await respond_json(writer, {"ok": ok, "message": "OK" if ok else reason, "show": show_name, "seconds": seconds})
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
                result = trigger_event(name, seconds_override=seconds_override, now_ms=now)
                await respond_json(writer, result)
                return

            if route == "/api/progress_mode":
                on = as_str(params, "on", "0").lower()
                progress_mode_enabled = on in ("1", "true", "yes", "on")
                await respond_json(writer, {"ok": True, "message": "Progress mode updated", "progress_mode_enabled": progress_mode_enabled})
                return

            if route == "/api/progress_config":
                s = as_int(params, "start", progress_start)
                e = as_int(params, "end", progress_end)
                # clamp to strip
                progress_start = int(clamp(s, 0, NEOPIXEL_COUNT - 1))
                progress_end = int(clamp(e, 0, NEOPIXEL_COUNT - 1))
                # refresh progress pixel map
                progress_effect._pixels = get_progress_pixels()
                await respond_json(writer, {
                    "ok": True,
                    "message": "Progress config updated",
                    "progress_start": progress_start,
                    "progress_end": progress_end
                })
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

            await respond_json(writer, {"ok": False, "message": "Unknown API route"}, code="404 Not Found")
            return

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
