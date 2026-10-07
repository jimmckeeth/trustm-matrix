# SPDX-License-Identifier: AGPL-3.0-or-later
"""brownian: random walkers leaving fading trails.

A handful of walkers each take one truly-random step (-1, 0 or +1 in x and
in y, drawn from the Trust M's TRNG) every tick, wrapping around the panel
edges. Each walker remembers its last TRAIL positions and draws them fading
from its full color at the head down to black at the tail, so the faster the
tick delay, the faster the whole field swarms. Each walker's color is a
random TRNG color pushed to full brightness.
"""

import time

from vizcore import HEIGHT, WIDTH, scale_color

WALKERS = 10
TRAIL = 36  # positions remembered per walker (head included)


class Brownian:
    NAME = "brownian"

    def __init__(self, env):
        self.rng = env.rng
        self.pal = env.canvas.palette
        rng = self.rng

        self.x = [rng.below(WIDTH) for _ in range(WALKERS)]
        self.y = [rng.below(HEIGHT) for _ in range(WALKERS)]
        # Ring buffer of palette indices per walker; -1 = nothing there yet.
        self.trail = [[-1] * TRAIL for _ in range(WALKERS)]
        self.head = [0] * WALKERS
        # fade[w][age]: age 0 is the head (full color); the trail dims
        # quadratically so it reads as a comet tail rather than a stripe.
        self.fade = []
        for _ in range(WALKERS):
            color = rng.color()
            peak = max(color >> 16, (color >> 8) & 0xFF, color & 0xFF)
            color = scale_color(color, 255 / peak)  # full brightness
            row = []
            for age in range(TRAIL):
                f = (TRAIL - age) / TRAIL
                row.append(scale_color(color, f * f))
            self.fade.append(row)
        for w in range(WALKERS):
            self.trail[w][0] = self.y[w] * WIDTH + self.x[w]
        self.draw()
        self.next_tick = time.monotonic()

    def update(self, now, delay):
        if now >= self.next_tick:
            self.step()
            self.next_tick = now + delay

    def status(self):
        return "brownian, %d walkers" % WALKERS

    def step(self):
        below = self.rng.below
        pal = self.pal
        trail, head = self.trail, self.head
        for w in range(WALKERS):
            self.x[w] = (self.x[w] + below(3) - 1) % WIDTH
            self.y[w] = (self.y[w] + below(3) - 1) % HEIGHT
            # The slot after the head holds the oldest position: blank it,
            # then reuse it for the new head.
            h = (head[w] + 1) % TRAIL
            old = trail[w][h]
            if old >= 0:
                pal[old] = 0x000000
            trail[w][h] = self.y[w] * WIDTH + self.x[w]
            head[w] = h
        self.draw()

    def draw(self):
        # Oldest age first across all walkers, so heads always end up on top
        # where trails cross.
        pal = self.pal
        for age in range(TRAIL - 1, -1, -1):
            for w in range(WALKERS):
                idx = self.trail[w][(self.head[w] - age) % TRAIL]
                if idx >= 0:
                    pal[idx] = self.fade[w][age]
