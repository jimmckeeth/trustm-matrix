# SPDX-License-Identifier: AGPL-3.0-or-later
"""hue-frequency: a calmer, single-hue-at-a-time variation.

Every tick, a random lane (a column, or a row if gravity is pointing
sideways -- read from the onboard LIS3DH accelerometer) is chosen and a pixel
drops from the spawn edge in the current hue, landing on top of whatever is
already stacked against the far wall of that lane. The hue then advances to
the next color in HUE_COLORS (a fixed cycle through Tailwind's 17 "600"
shades, red through rose) for the next drop, wherever it lands.

When a lane's stack reaches the spawn edge (i.e. it's completely full), that
lane alone falls off the far wall and starts empty again -- independently of
every other lane, and without pausing new drops elsewhere.

No stack-height counter is kept: a lane's fill state lives entirely in
cell_state, and a drop settles by colliding with the grid.
"""

import time

from vizcore import HEIGHT, WIDTH

VERBOSE = False

FALL_STEP_PERIOD = 0.04  # seconds between one-cell steps of a falling pixel

# Tailwind v4 "600" shades, red-600 through rose-600 (17 hues, the full
# palette in its standard order) -- approximate sRGB hex for each, close
# enough given the matrix panel is only 6 bits per channel anyway.
HUE_COLORS = (
    0xDC2626,  # red-600
    0xEA580C,  # orange-600
    0xD97706,  # amber-600
    0xCA8A04,  # yellow-600
    0x65A30D,  # lime-600
    0x16A34A,  # green-600
    0x059669,  # emerald-600
    0x0D9488,  # teal-600
    0x0891B2,  # cyan-600
    0x0284C7,  # sky-600
    0x2563EB,  # blue-600
    0x4F46E5,  # indigo-600
    0x7C3AED,  # violet-600
    0x9333EA,  # purple-600
    0xC026D3,  # fuchsia-600
    0xDB2777,  # pink-600
    0xE11D48,  # rose-600
)

# OFF: black, spawnable. LIT: settled. ANIM: a particle in flight.
OFF, LIT, ANIM = range(3)

MAX_LANES = max(WIDTH, HEIGHT)


class HueFrequency:
    NAME = "hue-frequency"

    def __init__(self, env):
        self.rng = env.rng
        self.set_pixel = env.canvas.set_pixel

        # Orientation is read once, on entering the mode, rather than every
        # tick. Re-reading it live was a real bug: with the board held near a
        # borderline tilt, accelerometer noise can flip which axis reads as
        # "down" between two consecutive calls, and the lane bookkeeping below
        # (and whether a lane means a column or a row) is shared across many
        # ticks -- a flip mid-lane silently corrupts it (observed on hardware
        # as lanes reporting "full" after ~30 drops instead of 32). The cost
        # is not reacting to the board being rotated while in this mode --
        # switch away and back to re-read it.
        self.dx, self.dy = env.gravity.direction()

        self.cell_state = [[OFF] * WIDTH for _ in range(HEIGHT)]
        self.cell_color = [[0] * WIDTH for _ in range(HEIGHT)]
        self.hue_index = 0  # index into HUE_COLORS for the next drop
        self.dropping = [False] * MAX_LANES  # lane has a particle in flight or is clearing
        self.clearing = [False] * MAX_LANES  # lane is currently falling off
        self.clear_next_time = [0.0] * MAX_LANES
        self.falling = []  # list of [x, y, color, next_step_time]
        self.next_tick = time.monotonic()

    def update(self, now, delay):
        self.advance_falling(now)
        self.advance_clearing(now)
        if now >= self.next_tick:
            self.on_tick()
            self.next_tick = now + delay

    def status(self):
        return "hue-frequency, %d drops in flight" % len(self.falling)

    def on_tick(self):
        dx, dy = self.dx, self.dy
        nlanes = WIDTH if dy != 0 else HEIGHT
        lane = self.rng.below(nlanes)
        if dy != 0:
            spawn_x, spawn_y = lane, (0 if dy > 0 else HEIGHT - 1)
        else:
            spawn_x, spawn_y = (0 if dx > 0 else WIDTH - 1), lane
        if self.dropping[lane] or self.clearing[lane]:
            return  # that lane's busy; a later tick will pick another
        if self.cell_state[spawn_y][spawn_x] != OFF:
            self.start_clear(lane)  # stacked all the way to the spawn edge: full
            return
        color = HUE_COLORS[self.hue_index]
        self.hue_index = (self.hue_index + 1) % len(HUE_COLORS)
        self.dropping[lane] = True
        self.set_pixel(spawn_x, spawn_y, color)
        self.cell_state[spawn_y][spawn_x] = ANIM
        self.cell_color[spawn_y][spawn_x] = color
        self.falling.append([spawn_x, spawn_y, color, time.monotonic() + FALL_STEP_PERIOD])
        if VERBOSE:
            print("hue: drop lane=%d (%d,%d) #%06x" % (lane, spawn_x, spawn_y, color))

    def start_clear(self, lane):
        self.clearing[lane] = True
        self.dropping[lane] = True  # keep blocking new drops while it empties
        self.clear_next_time[lane] = time.monotonic()
        if VERBOSE:
            print("hue: lane %d full, falling off" % lane)

    def advance_falling(self, now):
        if not self.falling:
            return
        dx, dy = self.dx, self.dy
        cell_state = self.cell_state
        keep = []
        for particle in self.falling:
            x, y, color, next_step = particle
            if now < next_step:
                keep.append(particle)
                continue
            nx, ny = x + dx, y + dy
            if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT) or cell_state[ny][nx] != OFF:
                # blocked by a wall or another settled pixel -- this is where
                # it lands, so this is what must persist for the clear cascade
                # to later read the right color back out of the grid.
                cell_state[y][x] = LIT
                self.cell_color[y][x] = color
                lane = x if dy != 0 else y
                self.dropping[lane] = False
                continue
            self.set_pixel(x, y, 0x000000)
            cell_state[y][x] = OFF
            self.set_pixel(nx, ny, color)
            cell_state[ny][nx] = ANIM
            particle[0], particle[1], particle[3] = nx, ny, now + FALL_STEP_PERIOD
            keep.append(particle)
        self.falling[:] = keep

    def advance_clearing(self, now):
        dx, dy = self.dx, self.dy
        cell_state, cell_color = self.cell_state, self.cell_color
        for lane in range(MAX_LANES):
            if not self.clearing[lane]:
                continue
            if now < self.clear_next_time[lane]:
                continue
            self.clear_next_time[lane] = now + FALL_STEP_PERIOD
            moved_any = False
            # Leading-edge-first walk so a cell moved this pass isn't re-read
            # later in the same pass.
            if dy != 0:
                col = lane
                for y in range(HEIGHT - 1, -1, -1) if dy > 0 else range(HEIGHT):
                    if cell_state[y][col] != LIT:
                        continue
                    moved_any = True
                    color = cell_color[y][col]
                    self.set_pixel(col, y, 0x000000)
                    cell_state[y][col] = OFF
                    ny = y + dy
                    if 0 <= ny < HEIGHT:
                        self.set_pixel(col, ny, color)
                        cell_color[ny][col] = color
                        cell_state[ny][col] = LIT
            else:
                row = lane
                for x in range(WIDTH - 1, -1, -1) if dx > 0 else range(WIDTH):
                    if cell_state[row][x] != LIT:
                        continue
                    moved_any = True
                    color = cell_color[row][x]
                    self.set_pixel(x, row, 0x000000)
                    cell_state[row][x] = OFF
                    nx = x + dx
                    if 0 <= nx < WIDTH:
                        self.set_pixel(nx, row, color)
                        cell_color[row][nx] = color
                        cell_state[row][nx] = LIT
            if not moved_any:
                self.clearing[lane] = False
                self.dropping[lane] = False
