# SPDX-License-Identifier: AGPL-3.0-or-later
"""lava: a continuously evolving Perlin-noise field with a drifting palette.

Each frame, 3-D Perlin noise (x, y, time) is sampled on a coarse grid
(every SAMPLE pixels) at two octaves, and the samples are bilinearly
interpolated up to the full 64x32 panel -- sampling noise per pixel would be
far too slow in pure Python. The interpolated value indexes a small color
lookup table that is rebuilt every frame with a slowly rotating hue, so the
"lava" wanders through red/orange, then green, then blue... over time.

The Perlin permutation table is shuffled with bytes from the Trust M's TRNG,
so every boot gets a different, truly random field; the starting hue comes
from the TRNG too.
"""

import time

from vizcore import HEIGHT, WIDTH, floor, hsv

SAMPLE = 8  # pixels between coarse noise samples (must divide WIDTH and HEIGHT)
OCTAVE1 = 1 / 32  # lattice units per pixel: broad blobs
OCTAVE2 = 1 / 16  # finer detail, half the weight. Keep OCTAVE * SAMPLE <= 0.5
# (lattice units per coarse sample) or the coarse grid undersamples the noise.
TIME_STEP = 0.015  # noise-time advanced per frame
HUE_DRIFT = 0.002  # palette hue rotation per frame (1.0 == full wheel)
LUT_SIZE = 64
LUT_EVERY = 3  # rebuild the color table every Nth frame (hue drifts too slowly to notice)

GW = WIDTH // SAMPLE + 1  # coarse grid points across
GH = HEIGHT // SAMPLE + 1


def _fade(t):
    return t * t * t * (t * (t * 6 - 15) + 10)


def _grad(h, x, y, z):
    h &= 15
    u = x if h < 8 else y
    v = y if h < 4 else (x if h == 12 or h == 14 else z)
    return (u if (h & 1) == 0 else -u) + (v if (h & 2) == 0 else -v)


def noise3(p, x, y, z):
    """Ken Perlin's improved noise; p is a 512-entry permutation table.
    Returns roughly [-1, 1]."""
    xi, yi, zi = floor(x), floor(y), floor(z)
    x, y, z = x - xi, y - yi, z - zi
    xi, yi, zi = xi & 255, yi & 255, zi & 255
    u, v, w = _fade(x), _fade(y), _fade(z)
    a = p[xi] + yi
    aa = p[a] + zi
    ab = p[a + 1] + zi
    b = p[xi + 1] + yi
    ba = p[b] + zi
    bb = p[b + 1] + zi
    x1 = x - 1
    y1 = y - 1
    z1 = z - 1
    # lerp(t, a, b) == a + t * (b - a), unrolled
    g0 = _grad(p[aa], x, y, z)
    g1 = _grad(p[ba], x1, y, z)
    l00 = g0 + u * (g1 - g0)
    g0 = _grad(p[ab], x, y1, z)
    g1 = _grad(p[bb], x1, y1, z)
    l10 = g0 + u * (g1 - g0)
    g0 = _grad(p[aa + 1], x, y, z1)
    g1 = _grad(p[ba + 1], x1, y, z1)
    l01 = g0 + u * (g1 - g0)
    g0 = _grad(p[ab + 1], x, y1, z1)
    g1 = _grad(p[bb + 1], x1, y1, z1)
    l11 = g0 + u * (g1 - g0)
    m0 = l00 + v * (l10 - l00)
    m1 = l01 + v * (l11 - l01)
    return m0 + w * (m1 - m0)


class Lava:
    NAME = "lava"

    def __init__(self, env):
        rng = env.rng
        self.pal = env.canvas.palette

        # Fisher-Yates shuffle of 0..255 driven by the TRNG, doubled so the
        # noise function can index past 255 without wrapping.
        perm = list(range(256))
        for i in range(255, 0, -1):
            j = rng.below(i + 1)
            perm[i], perm[j] = perm[j], perm[i]
        self.perm = bytearray(perm + perm)

        self.hue = rng.unit()
        self.t = rng.unit() * 100  # start somewhere arbitrary in noise-time
        self.lut = [0] * LUT_SIZE
        self.frames = 0
        # Coarse sample grid, reused every frame (rows of GW values).
        self.grid = [[0.0] * GW for _ in range(GH)]
        self.next_tick = time.monotonic()

    def update(self, now, delay):
        if now >= self.next_tick:
            self.frame()
            self.next_tick = now + delay

    def status(self):
        return "lava, hue %.2f" % (self.hue % 1.0)

    def build_lut(self):
        """Black -> deep color -> bright -> pale hot core, around self.hue."""
        lut = self.lut
        top = LUT_SIZE - 1
        for k in range(LUT_SIZE):
            t = k / top
            val = min(1.0, t * 1.7)
            sat = 1.0 if t < 0.8 else 1.0 - (t - 0.8) * 3  # whiten the hottest bit
            lut[k] = hsv(self.hue + t * 0.16, sat, val)

    def sample(self):
        p = self.perm
        t = self.t
        grid = self.grid
        top = LUT_SIZE - 1e-3
        for j in range(GH):
            y = j * SAMPLE
            row = grid[j]
            for i in range(GW):
                x = i * SAMPLE
                n = noise3(p, x * OCTAVE1, y * OCTAVE1, t)
                n += 0.5 * noise3(p, x * OCTAVE2 + 31.4, y * OCTAVE2 + 27.1, t * 1.7)
                # n is roughly +-0.8 in practice; stretch to fill the palette, clamp.
                v = (0.5 + n * 0.7) * LUT_SIZE
                row[i] = 0.0 if v < 0 else (top if v > top else v)

    def frame(self):
        self.t += TIME_STEP
        self.hue += HUE_DRIFT
        if self.frames % LUT_EVERY == 0:
            self.build_lut()
        self.frames += 1
        self.sample()
        lut, pal, grid = self.lut, self.pal, self.grid
        inv = 1 / SAMPLE
        for j in range(GH - 1):
            top_row, bot_row = grid[j], grid[j + 1]
            for py in range(SAMPLE):
                ty = py * inv
                base = (j * SAMPLE + py) * WIDTH
                for i in range(GW - 1):
                    # Vertical lerp at this cell's left and right edges, then
                    # step horizontally between them.
                    left = top_row[i] + (bot_row[i] - top_row[i]) * ty
                    right = top_row[i + 1] + (bot_row[i + 1] - top_row[i + 1]) * ty
                    step = (right - left) * inv
                    o = base + i * SAMPLE
                    pal[o] = lut[int(left)]
                    pal[o + 1] = lut[int(left + step)]
                    pal[o + 2] = lut[int(left + step * 2)]
                    pal[o + 3] = lut[int(left + step * 3)]
                    pal[o + 4] = lut[int(left + step * 4)]
                    pal[o + 5] = lut[int(left + step * 5)]
                    pal[o + 6] = lut[int(left + step * 6)]
                    pal[o + 7] = lut[int(left + step * 7)]
