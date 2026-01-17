# Pico Poster Lights (NeoPixel + Web Control)

A Raspberry Pi Pico W + NeoPixel controller for a “cinema poster frame” style light border.

Features:
- Non-blocking, tick-based animations (keeps HTTP responsive)
- Web UI (single-page controls; no navigation on button press)
- JSON API under `/api/*` for external triggers (e.g., a Raspberry Pi smart-light server)
- Semantic events (`/api/event`) so external controllers don’t need to know show names
- Progress bar mode (`/api/progress`) for “now playing” style integrations (e.g., Jellyfin via a separate Pi)

## Hardware
- Raspberry Pi Pico W
- NeoPixel / WS2812 string (default: 20 pixels)
- 5V power supply suitable for your pixel count
- Common ground between Pico and the LED power supply

## Setup
1. Copy the main script onto the Pico (e.g. `main.py` or run manually).
2. Create `config.py` on the Pico with your Wi-Fi credentials:

```python
REMOTE_SSID = "your-wifi-ssid"
REMOTE_PASSWORD = "your-wifi-password"
