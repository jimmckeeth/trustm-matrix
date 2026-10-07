# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared plumbing for the visualizations: the LED canvas, the Trust M TRNG
wrapper, the accelerometer's gravity direction, and a little color math.

Every visualization module (viz_*.py) defines one class with this shape:

    class Something:
        NAME = "something"              # shown in the mode list / logs
        def __init__(self, env): ...    # env is an `Env`; start from a clean canvas
        def update(self, now, delay): ...  # called every main-loop pass
        def status(self): ...           # short string for the periodic status print

`update()` is expected to be non-blocking: it decides for itself whether a
tick is due (using `delay`, the user-selected seconds-per-tick) and advances
whatever animations are in flight. code.py owns the display refresh.
"""

import math

import displayio
from adafruit_matrixportal.matrix import Matrix

WIDTH = 64
HEIGHT = 32

# Flip these if the accelerometer's physical mounting doesn't match the
# panel's x/y once you see it running -- can't be verified without hardware.
ACCEL_INVERT_X = False
ACCEL_INVERT_Y = False


class Canvas:
    """The 64x32 matrix. Every one of the 2048 cells gets its own dedicated
    `displayio.Palette` index (bitmap value == y*WIDTH+x, fixed at setup and
    never changed), so any two pixels can hold independent 24-bit colors with
    no palette-size limit -- a color change is just `palette[i] = color`.
    Visualizations that touch many pixels per frame may write `palette[i]`
    directly (i = y*WIDTH+x) instead of going through `set_pixel`."""

    def __init__(self):
        matrix = Matrix(width=WIDTH, height=HEIGHT, bit_depth=6)
        self.display = matrix.display
        # Default auto_refresh composites bitmap/palette writes continuously
        # in the background, so a multi-pixel update can be split across two
        # composites and show up as a half-updated frame. Driving refresh
        # manually, once per main-loop pass after all of that pass's writes,
        # makes each displayed frame atomic -- effectively double buffering.
        self.display.auto_refresh = False

        self.bitmap = displayio.Bitmap(WIDTH, HEIGHT, WIDTH * HEIGHT)
        self.palette = displayio.Palette(WIDTH * HEIGHT)
        for y in range(HEIGHT):
            for x in range(WIDTH):
                self.bitmap[x, y] = y * WIDTH + x
        self.clear()

        group = displayio.Group()
        group.append(displayio.TileGrid(self.bitmap, pixel_shader=self.palette))
        self.display.root_group = group

    def set_pixel(self, x, y, color):
        self.palette[y * WIDTH + x] = color

    def clear(self):
        for i in range(WIDTH * HEIGHT):
            self.palette[i] = 0x000000

    def refresh(self):
        self.display.refresh()


class TrueRandom:
    """Buffers TRNG bytes from the Trust M so a draw only hits the chip when
    the buffer runs dry, not once per pixel."""

    def __init__(self, chip, chunk=64):
        self._chip = chip
        self._chunk = chunk
        self._buf = bytearray()

    def _fill(self, buf):
        while len(self._buf) < len(buf):
            self._buf += self._chip.random(self._chunk)
        buf[:] = self._buf[: len(buf)]
        self._buf = self._buf[len(buf) :]

    def byte(self):
        b = bytearray(1)
        self._fill(b)
        return b[0]

    def pixel(self):
        # WIDTH and HEIGHT are both powers of two, so masking a random byte
        # is exactly uniform -- no modulo bias to worry about.
        b = bytearray(2)
        self._fill(b)
        return b[0] & (WIDTH - 1), b[1] & (HEIGHT - 1)

    def color(self):
        b = bytearray(3)
        self._fill(b)
        if b[0] == 0 and b[1] == 0 and b[2] == 0:
            b[0] = 1  # never hand back pure black, that means "off" here
        return (b[0] << 16) | (b[1] << 8) | b[2]

    def unit(self):
        """Uniform float in [0, 1) with 16 bits of resolution."""
        b = bytearray(2)
        self._fill(b)
        return ((b[0] << 8) | b[1]) / 65536

    def signed(self):
        """Uniform float in [-1, 1)."""
        return self.unit() * 2 - 1

    def jitter(self, max_seconds):
        return self.byte() / 255 * max_seconds

    def below(self, n):
        # Uniform in [0, n) via rejection sampling -- avoids the modulo bias
        # a plain `byte % n` would have for n that isn't a power of two.
        limit = 256 - (256 % n)
        while True:
            b = self.byte()
            if b < limit:
                return b % n


class Gravity:
    """Which way is "down" on the panel, from the onboard LIS3DH."""

    def __init__(self, accel):
        self._accel = accel

    def direction(self):
        try:
            ax, ay, _az = self._accel.acceleration
        except OSError:
            return (0, 1)  # sensor hiccup: fall toward the last row as a default
        if ACCEL_INVERT_X:
            ax = -ax
        if ACCEL_INVERT_Y:
            ay = -ay
        if abs(ax) > abs(ay):
            return (1, 0) if ax > 0 else (-1, 0)
        return (0, 1) if ay > 0 else (0, -1)


class Env:
    """Everything a visualization needs, handed to its constructor."""

    def __init__(self, canvas, rng, gravity):
        self.canvas = canvas
        self.rng = rng
        self.gravity = gravity


def hsv(h, s, v):
    """h wraps (any float, 1.0 == a full turn of the wheel); s and v in
    [0, 1]. Returns a packed 24-bit RGB int."""
    h = (h % 1.0) * 6
    i = int(h)
    f = h - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def scale_color(color, f):
    """Multiply a packed RGB color's brightness by f (0..1)."""
    r = int(((color >> 16) & 0xFF) * f)
    g = int(((color >> 8) & 0xFF) * f)
    b = int((color & 0xFF) * f)
    return (r << 16) | (g << 8) | b


def floor(x):
    return int(math.floor(x))
