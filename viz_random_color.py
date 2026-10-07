# SPDX-License-Identifier: AGPL-3.0-or-later
"""random-color: truly-random pixels light up in truly-random colors.

  - Starts all black.
  - Every tick, a truly-random pixel is chosen.
      - If it is currently off, it lights up in a truly-random color.
      - If it is already lit, it blinks (see ENABLE_FALLING below).
  - Once FULL_THRESHOLD of the board's cells are lit at the same time,
    normal ticking pauses and the board empties in one synchronized sweep:
    pixels are grouped into columns (or rows, if gravity is pointing
    sideways). Groups start falling one at a time, in random order, each one
    after a short random delay past the previous -- so at any moment several
    columns are smoothly falling together, just staggered in when they began.

ENABLE_FALLING toggles what happens when a lit pixel is revisited:
  - True:  it blinks twice, then tries to fall off the display in the
    direction gravity is currently pointing (read from the onboard LIS3DH
    accelerometer), so the matrix can be held or mounted at any angle and
    pixels still "fall" the right way. If its path is blocked by another lit
    pixel it settles in place instead (like sand piling up) and can be
    picked again later.
  - False (current default): it just blinks once and stays lit, so nothing
    removes cells between ticks -- this lets the board reliably fill up to
    FULL_THRESHOLD and makes the synchronized sweep easy to see.
"""

import gc
import time

from vizcore import HEIGHT, WIDTH

VERBOSE = False  # per-tick logging; noisy at fast delays

# Temporarily disabled so the board fills up (instead of pixels constantly
# falling off individually) and the full-board sweep is easy to see.
# Flip back to True to restore "revisit a lit pixel -> blink twice -> fall".
ENABLE_FALLING = False

BLINK_PERIOD = 0.12  # seconds per on/off phase, twice = ~0.48s total
FALL_STEP_PERIOD = 0.04  # seconds between one-cell steps of a falling pixel
SWEEP_STAGGER_MAX = 0.15  # seconds, max random gap before the next column/row starts
FULL_THRESHOLD = 0.75  # fraction of cells lit that triggers the full sweep

# OFF: black, spawnable. LIT: steady color, revisiting it starts a blink.
# ANIM: currently owned by a blink or a falling particle -- not spawnable,
# revisiting it is a no-op (it's already mid-animation).
OFF, LIT, ANIM = range(3)

MAX_GROUPS = max(WIDTH, HEIGHT)


class Blinker:
    """Flashes a pixel's existing color off/on, then either starts it falling
    (ENABLE_FALLING) or just leaves it lit in place (single blink, used while
    falling is disabled so the board can fill toward FULL_THRESHOLD)."""

    def __init__(self, owner, x, y, color, now):
        self.owner = owner
        self.x = x
        self.y = y
        self.color = color
        self.phase = 0
        self.last_phase = 3 if ENABLE_FALLING else 1
        self.next_time = now

    def advance(self, now):
        """Returns True when finished (caller removes it from the list)."""
        owner = self.owner
        if self.phase % 2 == 0:
            owner.set_pixel(self.x, self.y, 0x000000)
        else:
            owner.set_pixel(self.x, self.y, self.color)
        if self.phase == self.last_phase:
            if ENABLE_FALLING:
                owner.start_fall(self.x, self.y, self.color)
            else:
                owner.cell_state[self.y][self.x] = LIT
            return True
        self.phase += 1
        self.next_time = now + BLINK_PERIOD
        return False


class RandomColor:
    NAME = "random-color"

    def __init__(self, env):
        self.rng = env.rng
        self.gravity = env.gravity
        self.set_pixel = env.canvas.set_pixel

        self.cell_state = [[OFF] * WIDTH for _ in range(HEIGHT)]
        self.cell_color = [[0] * WIDTH for _ in range(HEIGHT)]
        self.lit_count = 0
        self.next_tick = time.monotonic()

        self.blinks = []  # active Blinker instances
        self.falling = []  # list of [x, y, color, next_step_time]

        # The sweep never allocates per-cell state: allocating a tuple per lit
        # cell (up to ~2048) at high fill raised a bare MemoryError on this
        # board's limited RAM. Group timing is just a few small arrays sized
        # by WIDTH/HEIGHT (<=64 entries), and advance_sweep walks the
        # cell_state/cell_color grids in place.
        self.sweep_active = False
        self.sweep_dx = 0
        self.sweep_dy = 0
        self.sweep_ngroups = 0
        self.sweep_order = list(range(MAX_GROUPS))  # random group activation order
        self.sweep_next_idx = 0  # index into sweep_order for the next group
        self.sweep_next_start_time = 0.0
        self.sweep_started = [False] * MAX_GROUPS
        self.sweep_group_start = [0.0] * MAX_GROUPS
        self.sweep_offset = [0] * MAX_GROUPS  # per-group steps taken so far
        self.sweep_due = [False] * MAX_GROUPS  # scratch: groups moving this tick
        self.sweep_remaining = 0  # cells still on-display; sweep ends at 0

    def update(self, now, delay):
        self.advance_blinks(now)
        self.advance_falling(now)
        self.advance_sweep(now)
        if not self.sweep_active and now >= self.next_tick:
            self.on_tick()
            self.next_tick = now + delay

    def status(self):
        return "%d/%d lit (%.0f%%)" % (
            self.lit_count,
            WIDTH * HEIGHT,
            100 * self.lit_count / (WIDTH * HEIGHT),
        )

    def check_full(self):
        if self.lit_count >= WIDTH * HEIGHT * FULL_THRESHOLD and not self.sweep_active:
            self.start_sweep()

    def on_tick(self):
        x, y = self.rng.pixel()
        state = self.cell_state[y][x]
        if state == OFF:
            color = self.rng.color()
            self.cell_state[y][x] = LIT
            self.cell_color[y][x] = color
            self.set_pixel(x, y, color)
            self.lit_count += 1
            if VERBOSE:
                print("lit (%d,%d) #%06x  lit_count=%d" % (x, y, color, self.lit_count))
            self.check_full()
        elif state == LIT:
            self.cell_state[y][x] = ANIM
            if ENABLE_FALLING:
                self.lit_count -= 1
            if VERBOSE:
                print("blink (%d,%d)  lit_count=%d" % (x, y, self.lit_count))
            self.blinks.append(Blinker(self, x, y, self.cell_color[y][x], time.monotonic()))
        # else ANIM: already mid blink/fall, nothing to do this tick

    # --- individual falls (triggered by revisiting a lit pixel) -----------
    def start_fall(self, x, y, color):
        self.falling.append([x, y, color, time.monotonic() + FALL_STEP_PERIOD])

    def advance_falling(self, now):
        if not self.falling:
            return
        dx, dy = self.gravity.direction()
        cell_state = self.cell_state
        keep = []
        for particle in self.falling:
            x, y, color, next_step = particle
            if now < next_step:
                keep.append(particle)
                continue
            nx, ny = x + dx, y + dy
            if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
                self.set_pixel(x, y, 0x000000)  # fell off the edge, gone for good
                cell_state[y][x] = OFF
                continue
            if cell_state[ny][nx] != OFF:
                # blocked -- give up and settle here as an ordinary lit pixel,
                # so a future tick can revisit it and try falling again,
                # rather than leaving it stuck invisible forever
                cell_state[y][x] = LIT
                self.cell_color[y][x] = color
                self.lit_count += 1
                self.check_full()
                continue
            self.set_pixel(x, y, 0x000000)
            cell_state[y][x] = OFF
            self.set_pixel(nx, ny, color)
            cell_state[ny][nx] = ANIM
            particle[0], particle[1], particle[3] = nx, ny, now + FALL_STEP_PERIOD
            keep.append(particle)
        self.falling[:] = keep

    def advance_blinks(self, now):
        if not self.blinks:
            return
        keep = []
        for b in self.blinks:
            if now >= b.next_time and b.advance(now):
                continue
            keep.append(b)
        self.blinks[:] = keep

    # --- full-board sweep: everything slides off together, grouped by column
    # (or row, if gravity points sideways). Groups start falling one at a
    # time, in random order, staggered by a short random delay each ---------
    def start_sweep(self):
        gc.collect()  # reclaim fragmented garbage before the sweep runs
        self.sweep_active = True
        # The sweep takes exclusive ownership of every non-off cell, including
        # ones mid-blink or mid-fall -- clearing these lists stops
        # advance_blinks / advance_falling from also touching them.
        self.blinks.clear()
        self.falling.clear()
        self.sweep_dx, self.sweep_dy = self.gravity.direction()
        self.sweep_ngroups = WIDTH if self.sweep_dy != 0 else HEIGHT
        # Random activation order: groups start one at a time in this order,
        # each staggered from the last by a short random delay (see
        # advance_sweep), rather than every group getting an independent
        # random start time up front -- that's what makes them visibly fall
        # together, just staggered, instead of arriving in a scattered wave.
        order = self.sweep_order
        for k in range(self.sweep_ngroups):
            order[k] = k
        for i in range(self.sweep_ngroups - 1, 0, -1):
            j = self.rng.below(i + 1)
            order[i], order[j] = order[j], order[i]
        for k in range(self.sweep_ngroups):
            self.sweep_started[k] = False
            self.sweep_offset[k] = 0
        self.sweep_next_idx = 0
        self.sweep_next_start_time = time.monotonic()  # first group starts now
        self.sweep_remaining = 0
        for y in range(HEIGHT):
            for x in range(WIDTH):
                if self.cell_state[y][x] != OFF:
                    self.cell_state[y][x] = ANIM
                    self.sweep_remaining += 1
        print(
            "sweep: %d/%d (%.0f%%) lit, clearing %d groups"
            % (
                self.lit_count,
                WIDTH * HEIGHT,
                100 * self.lit_count / (WIDTH * HEIGHT),
                self.sweep_ngroups,
            )
        )

    def advance_sweep(self, now):
        if not self.sweep_active:
            return
        ngroups = self.sweep_ngroups
        # Kick off however many groups have come due since the last call (in
        # the random order chosen by start_sweep), each queued a short random
        # gap after the one before it.
        while self.sweep_next_idx < ngroups and now >= self.sweep_next_start_time:
            k = self.sweep_order[self.sweep_next_idx]
            self.sweep_started[k] = True
            self.sweep_group_start[k] = now
            self.sweep_offset[k] = 0
            self.sweep_next_idx += 1
            self.sweep_next_start_time = now + self.rng.jitter(SWEEP_STAGGER_MAX)
        any_due = False
        for k in range(ngroups):
            if not self.sweep_started[k]:
                self.sweep_due[k] = False
                continue
            due = now >= self.sweep_group_start[k] + self.sweep_offset[k] * FALL_STEP_PERIOD
            self.sweep_due[k] = due
            if due:
                any_due = True
                self.sweep_offset[k] += 1
        if not any_due:
            return
        dx, dy = self.sweep_dx, self.sweep_dy
        cell_state, cell_color = self.cell_state, self.cell_color
        sweep_due = self.sweep_due
        # Walk the grid leading-edge-first so a cell moved this tick is never
        # re-read and moved again later in the same pass.
        y_range = range(HEIGHT - 1, -1, -1) if dy > 0 else range(HEIGHT)
        x_range = range(WIDTH - 1, -1, -1) if dx > 0 else range(WIDTH)
        for y in y_range:
            for x in x_range:
                if cell_state[y][x] != ANIM:
                    continue
                key = x if dy != 0 else y
                if not sweep_due[key]:
                    continue
                color = cell_color[y][x]
                self.set_pixel(x, y, 0x000000)
                cell_state[y][x] = OFF
                nx, ny = x + dx, y + dy
                if 0 <= nx < WIDTH and 0 <= ny < HEIGHT:
                    self.set_pixel(nx, ny, color)
                    cell_color[ny][nx] = color
                    cell_state[ny][nx] = ANIM
                else:
                    self.sweep_remaining -= 1
        if self.sweep_remaining <= 0:
            self.sweep_active = False
            self.lit_count = 0
            print("sweep: done, board clear")
