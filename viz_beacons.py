# SPDX-License-Identifier: AGPL-3.0-or-later
"""beacons: five public randomness beacons, one panel region each.

The 64x32 panel is split into five side-by-side regions (8, 18, 8, 18, 8
pixels wide, one blank pixel column between them). Within each region the
bottom pixel row is a countdown bar to the beacon's next period; the rows
above are filled with colors derived from the beacon's latest random value,
and repaint whenever a new value is published:

    evmnet (drand, 3s)   NIST (60s)   drand default (30s)   INMETRO (60s)   quicknet (drand, 3s)

The beacons' values are only 48-64 bytes, far less than the 24 bits x
~250-560 pixels a region needs, so each value seeds a small xorshift128
generator that expands it into the pixel colors. The mapping is fully
deterministic: the same beacon value always paints the same picture.

Needs WiFi (CIRCUITPY_WIFI_SSID / CIRCUITPY_WIFI_PASSWORD in settings.toml).
Network fetches block the main loop for up to a second or two, during which
the countdown bars pause (the LEDs keep showing the last frame). The shared
tick `delay` from the buttons is not used by this mode.
"""

import gc
import os
import sys
import time

import adafruit_connection_manager
import adafruit_requests
import board
import digitalio
from adafruit_esp32spi import adafruit_esp32spi

from vizcore import HEIGHT, WIDTH

# (label, url, kind, fixed period in seconds). drand periods/genesis are read
# from the beacon's own /info; NIST and INMETRO pulses land on the minute.
BEACONS = (
    ("evmnet", "https://api.drand.sh/v2/beacons/evmnet/rounds/latest", "drand", 3),
    ("nist", "https://beacon.nist.gov/beacon/2.0/pulse/last", "pulse", 60),
    ("default", "https://api.drand.sh/v2/beacons/default/rounds/latest", "drand", 30),
    ("inmetro", "https://beacon.inmetro.gov.br/beacon/2.1/pulse/last", "pulse", 60),
    ("quicknet", "https://api.drand.sh/v2/beacons/quicknet/rounds/latest", "drand", 3),
)
REGION_WIDTHS = (8, 18, 8, 18, 8)  # + 4 one-pixel gaps == 64
TIMER_ROW = HEIGHT - 1

BAR_COLOR = 0x404040
ERROR_COLOR = 0x400000
GRACE = 0.5  # seconds after a period boundary before the first fetch
RETRY = {"drand": 1.0, "pulse": 2.0}  # seconds between polls while waiting for a new value
ERROR_RETRY = 5.0
HTTP_TIMEOUT = 6
CLOCK_RESYNC = 600  # seconds between re-reading the ESP32's NTP clock

MASK = 0xFFFFFFFF
CLOCK_URL = "https://api.drand.sh/v2/chains"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def expand(data, count):
    """Expand beacon bytes into `count` 24-bit colors: fold the bytes into a
    128-bit xorshift state, then draw 24 bits per pixel."""
    s = [0x9E3779B9, 0x243F6A88, 0xB7E15162, 0x6A09E667]
    for i, b in enumerate(data):
        k = i & 3
        s[k] = ((((s[k] << 7) | (s[k] >> 25)) & MASK) ^ (b * 0x01000193) ^ i) & MASK
    colors = []
    x, y, z, w = s
    for n in range(count + 16):
        t = x ^ ((x << 11) & MASK)
        x, y, z = y, z, w
        w = w ^ (w >> 19) ^ t ^ (t >> 8)
        if n >= 16:  # discard warm-up outputs
            colors.append(w >> 8)
    return colors


class Network:
    """ESP32 co-processor WiFi + HTTPS (the Matrix Portal has no native WiFi)."""

    def __init__(self):
        self.ssid = os.getenv("CIRCUITPY_WIFI_SSID")
        self.password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
        self.pins = (
            digitalio.DigitalInOut(board.ESP_CS),
            digitalio.DigitalInOut(board.ESP_BUSY),
            digitalio.DigitalInOut(board.ESP_RESET),
        )
        self.esp = adafruit_esp32spi.ESP_SPIcontrol(board.SPI(), *self.pins)
        pool = adafruit_connection_manager.get_radio_socketpool(self.esp)
        ssl = adafruit_connection_manager.get_radio_ssl_context(self.esp)
        self.session = adafruit_requests.Session(pool, ssl)
        # CircuitPython floats are single-precision (~7 digits), far too coarse
        # for epoch seconds (~1.8e9). So all scheduling uses `t`, seconds since
        # startup (small), plus an exact integer `offset` with epoch == offset + int(t).
        self.ns0 = time.monotonic_ns()
        self.offset = 0
        self.synced_at = -1e9

    def connect(self):
        if self.esp.is_connected:
            return
        print("wifi: connecting to", self.ssid)
        self.esp.connect_AP(self.ssid, self.password)
        print("wifi: connected, ip", self.esp.pretty_ip(self.esp.ip_address))

    def sync_clock(self):
        """Wall-clock epoch seconds: try the ESP32's NTP clock, else fall
        back to the Date header of an HTTPS response (1s resolution)."""
        for _ in range(3):
            try:
                t = self.esp.get_time()
                t = t[0] if isinstance(t, (tuple, list)) else t
                if t > 1_600_000_000:
                    self._set_clock(t)
                    return
            except OSError:
                pass
            time.sleep(0.5)
        self.connect()
        resp = self.session.get(CLOCK_URL, timeout=HTTP_TIMEOUT)
        try:
            date = resp.headers["date"]  # "Tue, 06 Oct 2026 12:00:00 GMT"
        finally:
            resp.close()
        _, d, mon, y, hms, _ = date.split()
        hh, mm, ss = hms.split(":")
        month = MONTHS.index(mon) + 1
        self._set_clock(time.mktime((int(y), month, int(d), int(hh), int(mm), int(ss), 0, 0, -1)))

    def _set_clock(self, epoch):
        t = self._t()
        self.offset = int(epoch) - int(t)
        self.synced_at = t

    def _t(self):
        return (time.monotonic_ns() - self.ns0) / 1e9

    def now(self):
        """Seconds since startup (float), resyncing the epoch offset every CLOCK_RESYNC."""
        t = self._t()
        if t - self.synced_at > CLOCK_RESYNC:
            try:
                self.sync_clock()
            except Exception:
                if self.synced_at < 0:
                    raise
                self.synced_at = t  # keep free-running, try again later
        return t

    def boundary(self, genesis, period, t):
        """Local time (int seconds since startup) of the next period start."""
        e = self.offset + int(t)
        return genesis + ((e - genesis) // period + 1) * period - self.offset

    def close(self):
        """Release the ESP32's pins and the WiFi/HTTP libraries' RAM so this
        mode can be entered again and the next mode has the memory back."""
        try:
            adafruit_connection_manager.connection_manager_close_all(release_references=True)
        except Exception:
            pass
        for pin in self.pins:
            pin.deinit()
        self.session = self.esp = None
        for name in list(sys.modules):
            if name.startswith(("adafruit_esp32spi", "adafruit_requests", "adafruit_connection_manager")):
                del sys.modules[name]

    def get_json(self, url):
        gc.collect()
        self.connect()
        resp = self.session.get(url, timeout=HTTP_TIMEOUT)
        try:
            return resp.json()
        finally:
            resp.close()
            gc.collect()


class Region:
    def __init__(self, canvas_pal, label, url, kind, period, x0, width):
        self.pal = canvas_pal
        self.label = label
        self.url = url
        self.kind = kind
        self.period = period
        self.genesis = 0
        self.x0 = x0
        self.width = width
        self.last_id = None
        self.due = 0.0  # local time (see Network) at which to (next) fetch; 0 == immediately
        self.error = False
        self.lit = -1  # bar pixels currently drawn
        # The bar belongs to the value on screen: full when that value is drawn
        # (start), draining to empty at the next expected update (deadline), then
        # staying empty until the new value actually arrives.
        self.start = 0.0
        self.deadline = 0.0

    def load_info(self, net):
        if self.kind == "drand":
            info = net.get_json(self.url.replace("rounds/latest", "info"))
            self.period = int(info["period"])
            self.genesis = int(info["genesis_time"])

    def fetch(self, net, t):
        data = net.get_json(self.url)
        if self.kind == "drand":
            ident, hexval = data["round"], data["signature"]
        else:
            ident = hexval = data["pulse"]["outputValue"]
        if ident == self.last_id:
            self.due = t + RETRY[self.kind]
            return False
        self.last_id = ident
        self.draw(bytes.fromhex(hexval))
        self.due = net.boundary(self.genesis, self.period, t) + GRACE
        self.start = t
        self.deadline = self.due
        return True

    def draw(self, value):
        w = self.width
        colors = expand(value, w * TIMER_ROW)
        pal = self.pal
        i = 0
        for y in range(TIMER_ROW):
            o = y * WIDTH + self.x0
            for x in range(w):
                pal[o + x] = colors[i]
                i += 1

    def draw_bar(self, t):
        span = self.deadline - self.start
        lit = int((self.deadline - t) / span * self.width + 0.999) if span > 0 else 0
        lit = max(0, min(self.width, lit))
        if lit == self.lit and not self.error:
            return
        self.lit = lit
        color = ERROR_COLOR if self.error else BAR_COLOR
        o = TIMER_ROW * WIDTH + self.x0
        for x in range(self.width):
            self.pal[o + x] = color if x < lit else 0x000000


class Beacons:
    NAME = "beacons"

    def __init__(self, env):
        pal = env.canvas.palette
        self.net = Network()
        self.net.connect()
        self.net.sync_clock()
        self.regions = []
        x = 0
        for (label, url, kind, period), width in zip(BEACONS, REGION_WIDTHS):
            self.regions.append(Region(pal, label, url, kind, period, x, width))
            x += width + 1
        for r in self.regions:  # drand periods + genesis come from the beacon
            try:
                r.load_info(self.net)
            except Exception as e:
                print("info failed:", r.label, repr(e))
                r.error = True
        self.updates = 0

    def update(self, now, delay):
        t = self.net.now()
        # One blocking fetch per pass keeps the bars moving; take the most
        # overdue region so the fast 3s beacons can't starve the slow ones.
        due = None
        for r in self.regions:
            if t >= r.due and (due is None or r.due < due.due):
                due = r
        if due is not None:
            r = due
            try:
                if r.kind == "drand" and not r.genesis:
                    r.load_info(self.net)
                if r.fetch(self.net, t):
                    self.updates += 1
                    print("beacon %s: new value" % r.label)
                r.error = False
            except Exception as e:
                print("beacon %s failed: %r" % (r.label, e))
                r.error = True
                r.due = t + ERROR_RETRY
                r.lit = -1
            t = self.net.now()
        for r in self.regions:
            r.draw_bar(t)

    def close(self):
        self.net.close()

    def status(self):
        return "beacons, %d updates, %d errors" % (
            self.updates,
            sum(1 for r in self.regions if r.error),
        )
