# SPDX-License-Identifier: AGPL-3.0-or-later
"""voronoi: drifting, evolving Voronoi cells.

SEEDS points wander around the panel (bouncing off the edges), their
velocities nudged by tiny random kicks every frame, and each pixel takes the
color of its nearest seed. Each seed's hue slowly drifts at its own rate and
direction, and its color is shaded darker with distance from the seed, so
every cell glows at its center and the borders between cells show as dark
seams. Start positions, velocities, hues and drift rates all come from the
Trust M's TRNG, as does every velocity kick.

Nearest-seed search is done on SCALE x SCALE blocks of pixels rather than
per pixel -- the full 64x32 x SEEDS search is too slow in pure Python.
"""

import time

from vizcore import HEIGHT, WIDTH, hsv

SEEDS = 8
SCALE = 2  # nearest-seed search block size (must divide WIDTH and HEIGHT)
MAX_SPEED = 0.35  # pixels per frame
KICK = 0.03  # max random velocity change per frame, per axis
HUE_RATE = 0.004  # max hue drift per frame per seed
SHADES = 8  # brightness steps from a seed's center out to its borders
SHADE_K = 0.35  # shade steps per squared block-distance

W2 = WIDTH // SCALE
H2 = HEIGHT // SCALE


class Voronoi:
    NAME = "voronoi"

    def __init__(self, env):
        rng = env.rng
        self.rng = rng
        self.pal = env.canvas.palette

        self.px = [rng.unit() * WIDTH for _ in range(SEEDS)]
        self.py = [rng.unit() * HEIGHT for _ in range(SEEDS)]
        self.vx = [rng.signed() * MAX_SPEED for _ in range(SEEDS)]
        self.vy = [rng.signed() * MAX_SPEED for _ in range(SEEDS)]
        self.hue = [rng.unit() for _ in range(SEEDS)]
        # Per-seed hue drift: random direction, never quite zero.
        self.rate = [
            (0.25 + 0.75 * rng.unit()) * HUE_RATE * (1 if rng.byte() & 1 else -1)
            for _ in range(SEEDS)
        ]
        self.shades = [[0] * SHADES for _ in range(SEEDS)]
        # Scratch buffers reused every frame to avoid allocation churn.
        self.dx2 = [[0.0] * W2 for _ in range(SEEDS)]
        self.sx = [0.0] * SEEDS
        self.sy = [0.0] * SEEDS
        self.dy2 = [0.0] * SEEDS
        self.next_tick = time.monotonic()

    def update(self, now, delay):
        if now >= self.next_tick:
            self.frame()
            self.next_tick = now + delay

    def status(self):
        return "voronoi, %d cells" % SEEDS

    def drift(self):
        rng = self.rng
        for i in range(SEEDS):
            vx = self.vx[i] + rng.signed() * KICK
            vy = self.vy[i] + rng.signed() * KICK
            vx = max(-MAX_SPEED, min(MAX_SPEED, vx))
            vy = max(-MAX_SPEED, min(MAX_SPEED, vy))
            x = self.px[i] + vx
            y = self.py[i] + vy
            if x < 0 or x >= WIDTH:
                vx = -vx
                x = max(0.0, min(WIDTH - 0.01, x))
            if y < 0 or y >= HEIGHT:
                vy = -vy
                y = max(0.0, min(HEIGHT - 0.01, y))
            self.vx[i], self.vy[i], self.px[i], self.py[i] = vx, vy, x, y
            self.hue[i] += self.rate[i]
            h = self.hue[i]
            shades = self.shades[i]
            for s in range(SHADES):
                shades[s] = hsv(h, 0.85, 1.0 - 0.8 * s / (SHADES - 1))

    def frame(self):
        self.drift()
        pal = self.pal
        shades, dx2, dy2 = self.shades, self.dx2, self.dy2
        # Seed positions in block coordinates; block centers sit at +0.5.
        inv = 1 / SCALE
        sx = [x * inv for x in self.px]
        sy = [y * inv for y in self.py]
        for i in range(SEEDS):
            row = dx2[i]
            s = sx[i] - 0.5
            for bx in range(W2):
                d = bx - s
                row[bx] = d * d
        top = SHADES - 1
        seeds = range(SEEDS)
        for by in range(H2):
            for i in seeds:
                d = by + 0.5 - sy[i]
                dy2[i] = d * d
            row0 = by * SCALE * WIDTH
            for bx in range(W2):
                best = 1e9
                bi = 0
                for i in seeds:
                    d = dx2[i][bx] + dy2[i]
                    if d < best:
                        best = d
                        bi = i
                s = int(best * SHADE_K)
                color = shades[bi][s if s < top else top]
                o = row0 + bx * SCALE
                for r in range(SCALE):
                    for c in range(SCALE):
                        pal[o + r * WIDTH + c] = color
