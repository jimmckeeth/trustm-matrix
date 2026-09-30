# SPDX-License-Identifier: AGPL-3.0-or-later
"""Random-pixel visualization for the 64x32 RGB matrix, seeded entirely by the
OPTIGA Trust M's hardware TRNG.

Two display variations, selectable at runtime (see button mapping below):

  "random-color" (default on boot):
    - Starts all black.
    - Every `delay` seconds, a truly-random pixel is chosen.
        - If it is currently off, it lights up in a truly-random color.
        - If it is already lit, it blinks (see ENABLE_FALLING below).
    - Once FULL_THRESHOLD of the board's cells are lit at the same time,
      normal ticking pauses and the board empties in one synchronized sweep:
      pixels are grouped into columns (or rows, if gravity is pointing
      sideways). Groups start falling one at a time, in random order, each
      one after a short random delay past the previous -- so at any moment
      several columns are smoothly falling together, just staggered in when
      they began.

  "hue-frequency":
    - Every `delay` seconds, a random lane (a column, or a row if gravity is
      pointing sideways -- read from the onboard LIS3DH accelerometer, same
      as ENABLE_FALLING below) is chosen and a pixel drops from the spawn
      edge in the current hue, landing on top of whatever is already stacked
      against the far wall of that lane. The hue then advances to the next
      color in HUE_COLORS (a fixed cycle through Tailwind's 17 "600" shades,
      red through rose) for the next drop, wherever it lands.
    - When a lane's stack reaches the spawn edge (i.e. it's completely full),
      that lane alone falls off the far wall and starts empty again --
      independently of every other lane, and without pausing new drops
      elsewhere. Falling and dropping are both driven by the same
      non-blocking step loop as "random-color", so many lanes can be
      dropping/falling/clearing at once with no visible stutter.

UP/DOWN buttons step the shared `delay` through the values in DELAYS
(applies to both variations); holding either button for LONG_PRESS_SECONDS
rotates to the next display variation instead.

ENABLE_FALLING (random-color mode only) toggles what happens when a lit
pixel is revisited:
  - True:  it blinks twice, then tries to fall off the display in the
    direction gravity is currently pointing (read from the onboard LIS3DH
    accelerometer), so the matrix can be held or mounted at any angle and
    pixels still "fall" the right way. If its path is blocked by another
    lit pixel it settles in place instead (like sand piling up) and can be
    picked again later. This is the continuous mechanism by which the field
    would otherwise return to black on its own.
  - False (current default): it just blinks once and stays lit, so nothing
    removes cells between ticks -- this lets the board reliably fill up to
    FULL_THRESHOLD and makes the synchronized sweep easy to see. Flip back
    to True to restore continuous individual falling.
"""

import gc
import time

import adafruit_lis3dh
import board
import digitalio
import displayio
from adafruit_matrixportal.matrix import Matrix

import trustm

WIDTH = 64
HEIGHT = 32
DELAYS = (0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10)
DELAY_INDEX_DEFAULT = 0
LONG_PRESS_SECONDS = 1.0  # hold UP/DOWN this long to switch display variation

VERBOSE = True  # per-tick logging; noisy at fast delays, flip off to quiet it
STATUS_PERIOD = 5  # seconds between periodic fill/free-memory status prints

# Temporarily disabled so the board fills up (instead of pixels constantly
# falling off individually) and the full-board sweep below is easy to see.
# Flip back to True to restore "revisit a lit pixel -> blink twice -> fall".
ENABLE_FALLING = False

BLINK_PERIOD = 0.12  # seconds per on/off phase, twice = ~0.48s total
FALL_STEP_PERIOD = 0.04  # seconds between one-cell steps of a falling pixel
SWEEP_STAGGER_MAX = 0.15  # seconds, max random gap before the next column/row starts
FULL_THRESHOLD = 0.75  # fraction of cells lit that triggers the full sweep

# Flip these if the accelerometer's physical mounting doesn't match the
# panel's x/y once you see it running -- can't be verified without hardware.
ACCEL_INVERT_X = False
ACCEL_INVERT_Y = False

# --- display variations -----------------------------------------------------
MODE_NAMES = ("random-color", "hue-frequency")
MODE_RANDOM_COLOR = 0
MODE_HUE_FREQUENCY = 1
mode_index = MODE_RANDOM_COLOR

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

# --- display: one dedicated palette slot per pixel, so every one of the
# 2048 cells can hold an independent random 24-bit color with no palette
# size limit ----------------------------------------------------------------
matrix = Matrix(width=WIDTH, height=HEIGHT, bit_depth=6)
display = matrix.display
# Default auto_refresh composites bitmap/palette writes into the matrix's
# framebuffer continuously in the background, so a multi-pixel update (e.g.
# every column moving one step during a sweep) can get split across two
# composites and show up as a half-updated frame. Driving refresh manually,
# once per main-loop tick after all of that tick's set_pixel calls are done,
# makes each displayed frame atomic -- effectively double buffering.
display.auto_refresh = False

bitmap = displayio.Bitmap(WIDTH, HEIGHT, WIDTH * HEIGHT)
palette = displayio.Palette(WIDTH * HEIGHT)
for y in range(HEIGHT):
    for x in range(WIDTH):
        bitmap[x, y] = y * WIDTH + x
for i in range(WIDTH * HEIGHT):
    palette[i] = 0x000000

group = displayio.Group()
group.append(displayio.TileGrid(bitmap, pixel_shader=palette))
display.root_group = group


def set_pixel(x, y, color):
    palette[y * WIDTH + x] = color


# --- shared STEMMA I2C bus: Trust M TRNG + onboard LIS3DH accelerometer ----
i2c = board.STEMMA_I2C()
chip = trustm.TrustM(i2c)
accel = adafruit_lis3dh.LIS3DH_I2C(i2c, address=0x19)


class TrueRandom:
    """Buffers TRNG bytes from the Trust M so every draw is a fresh chip
    call only when the buffer runs dry, not once per pixel."""

    def __init__(self, chip, chunk=64):
        self._chip = chip
        self._chunk = chunk
        self._buf = bytearray()

    def _fill(self, buf):
        while len(self._buf) < len(buf):
            self._buf += self._chip.random(self._chunk)
        buf[:] = self._buf[: len(buf)]
        self._buf = self._buf[len(buf) :]

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

    def jitter(self, max_seconds):
        b = bytearray(1)
        self._fill(b)
        return (b[0] / 255) * max_seconds

    def below(self, n):
        # Uniform in [0, n) via rejection sampling -- avoids the modulo bias
        # a plain `byte % n` would have for n that isn't a power of two.
        limit = 256 - (256 % n)
        b = bytearray(1)
        while True:
            self._fill(b)
            if b[0] < limit:
                return b[0] % n


rng = TrueRandom(chip)

# --- buttons: no external pull-ups on this board, pressed = low -----------
btn_up = digitalio.DigitalInOut(board.BUTTON_UP)
btn_up.switch_to_input(pull=digitalio.Pull.UP)
btn_down = digitalio.DigitalInOut(board.BUTTON_DOWN)
btn_down.switch_to_input(pull=digitalio.Pull.UP)

delay_index = DELAY_INDEX_DEFAULT
prev_up = True
prev_down = True
up_press_start = None
down_press_start = None
up_long_fired = False
down_long_fired = False


def poll_buttons():
    """Short tap (press then release before LONG_PRESS_SECONDS) steps the
    shared delay; holding past LONG_PRESS_SECONDS rotates the display
    variation instead and suppresses the delay-step on release. Actions now
    fire on release rather than press (needed to tell a tap from a hold)."""
    global delay_index, prev_up, prev_down
    global up_press_start, down_press_start, up_long_fired, down_long_fired
    now = time.monotonic()
    up = btn_up.value
    down = btn_down.value

    if prev_up and not up:  # just pressed
        up_press_start = now
        up_long_fired = False
    elif not up:  # held
        if not up_long_fired and now - up_press_start >= LONG_PRESS_SECONDS:
            switch_mode(1)
            up_long_fired = True
    elif not prev_up and up:  # just released
        if not up_long_fired:
            delay_index = min(delay_index + 1, len(DELAYS) - 1)
            print("delay ->", DELAYS[delay_index], "s")

    if prev_down and not down:
        down_press_start = now
        down_long_fired = False
    elif not down:
        if not down_long_fired and now - down_press_start >= LONG_PRESS_SECONDS:
            switch_mode(-1)
            down_long_fired = True
    elif not prev_down and down:
        if not down_long_fired:
            delay_index = max(delay_index - 1, 0)
            print("delay ->", DELAYS[delay_index], "s")

    prev_up, prev_down = up, down


# --- grid state -------------------------------------------------------------
# OFF: black, spawnable. LIT: steady color, revisiting it starts a blink.
# ANIM: currently owned by a blink or a falling particle -- not spawnable,
# revisiting it is a no-op (it's already mid-animation).
OFF, LIT, ANIM = range(3)
cell_state = [[OFF] * WIDTH for _ in range(HEIGHT)]
cell_color = [[0] * WIDTH for _ in range(HEIGHT)]
lit_count = 0

blinks = []  # active Blinker instances
falling = []  # list of [x, y, color, next_step_time]

sweep_active = False
# The sweep used to build a Python list of (x, y, color) tuples for every
# single lit cell (up to ~2048 of them) to track group membership. At a high
# fill level that allocation was big enough to raise a bare MemoryError on
# this board's limited RAM -- that's what was causing the "spontaneous"
# resets. Fixed by never allocating per-cell state at all: group timing is
# just a couple of small arrays sized by WIDTH/HEIGHT (<=64 entries), and
# advance_sweep walks the existing cell_state/cell_color grids in place.
MAX_GROUPS = max(WIDTH, HEIGHT)
sweep_dx = 0
sweep_dy = 0
sweep_ngroups = 0
sweep_order = list(range(MAX_GROUPS))  # random group activation order
sweep_next_idx = 0  # index into sweep_order for the next group to start
sweep_next_start_time = 0.0  # when that next group should start
sweep_started = [False] * MAX_GROUPS  # has this group begun falling yet
sweep_group_start = [0.0] * MAX_GROUPS  # per-group fall start time, once started
sweep_offset = [0] * MAX_GROUPS  # per-group steps taken so far
sweep_due = [False] * MAX_GROUPS  # scratch: which groups move this tick
sweep_remaining = 0  # cells still on-display; sweep ends when this hits 0

# --- hue-frequency mode state: one stack per lane (a column when gravity is
# vertical, a row when it's sideways -- same lane concept as the sweep's
# groups above). No stack-height counter is kept: a lane's fill state lives
# entirely in cell_state, and a drop settles by colliding with the grid
# exactly like the general per-pixel falling above -- this is what keeps it
# correct no matter which way the accelerometer says down is, including if
# it changes mid-animation. -------------------------------------------------
hue_index = 0  # index into HUE_COLORS for the next drop
hue_dropping = [False] * MAX_GROUPS  # lane has a particle in flight or is clearing
hue_clearing = [False] * MAX_GROUPS  # lane is currently falling off
hue_clear_next_time = [0.0] * MAX_GROUPS
hue_falling = []  # list of [x, y, color, next_step_time]


def gravity_direction():
    try:
        ax, ay, _az = accel.acceleration
    except OSError:
        return (0, 1)  # sensor hiccup: fall toward row 31 as a safe default
    if ACCEL_INVERT_X:
        ax = -ax
    if ACCEL_INVERT_Y:
        ay = -ay
    if abs(ax) > abs(ay):
        return (1, 0) if ax > 0 else (-1, 0)
    return (0, 1) if ay > 0 else (0, -1)


# hue-frequency's orientation, read once when the mode is (re)entered rather
# than every tick like the general per-pixel falling above does. Re-reading
# it live turned out to be a real bug: with the board held near a borderline
# tilt, accelerometer noise can flip which axis reads as "down" between two
# consecutive calls, and unlike a single falling particle (which just takes
# one wrong step and self-corrects), hue-frequency's lane bookkeeping
# (hue_dropping/hue_clearing indices, and whether a lane means a column or a
# row) is shared across many ticks -- a flip mid-lane silently corrupts it
# (observed on hardware as lanes reporting "full" after ~30 drops instead of
# the expected 32). Freezing it for as long as the mode is active avoids
# that entirely, at the cost of not reacting to being physically rotated
# while already in this mode -- switch away and back to re-read it.
hue_dx, hue_dy = gravity_direction()


def clear_display():
    global lit_count, sweep_active
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if cell_state[y][x] != OFF:
                set_pixel(x, y, 0x000000)
            cell_state[y][x] = OFF
            cell_color[y][x] = 0
    lit_count = 0
    blinks.clear()
    falling.clear()
    sweep_active = False
    hue_falling.clear()
    for k in range(MAX_GROUPS):
        hue_dropping[k] = False
        hue_clearing[k] = False


def switch_mode(direction):
    global mode_index, hue_index, hue_dx, hue_dy
    mode_index = (mode_index + direction) % len(MODE_NAMES)
    hue_index = 0
    if mode_index == MODE_HUE_FREQUENCY:
        hue_dx, hue_dy = gravity_direction()
    clear_display()
    print("mode ->", MODE_NAMES[mode_index])


def check_full():
    if lit_count >= WIDTH * HEIGHT * FULL_THRESHOLD and not sweep_active:
        start_sweep()


# --- individual falls (triggered by revisiting a lit pixel) ---------------
def start_fall(x, y, color):
    falling.append([x, y, color, time.monotonic() + FALL_STEP_PERIOD])


def advance_falling(now):
    global lit_count
    if not falling:
        return
    dx, dy = gravity_direction()
    keep = []
    for particle in falling:
        x, y, color, next_step = particle
        if now < next_step:
            keep.append(particle)
            continue
        nx, ny = x + dx, y + dy
        if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT):
            set_pixel(x, y, 0x000000)  # fell off the edge, gone for good
            cell_state[y][x] = OFF
            continue
        if cell_state[ny][nx] != OFF:
            # blocked -- give up and settle here as an ordinary lit pixel,
            # so a future tick can revisit it and try falling again, rather
            # than leaving it stuck invisible forever
            cell_state[y][x] = LIT
            cell_color[y][x] = color
            lit_count += 1
            check_full()
            continue
        set_pixel(x, y, 0x000000)
        cell_state[y][x] = OFF
        set_pixel(nx, ny, color)
        cell_state[ny][nx] = ANIM
        particle[0], particle[1], particle[3] = nx, ny, now + FALL_STEP_PERIOD
        keep.append(particle)
    falling[:] = keep


# --- non-blocking blink, then hand off to falling ---------------------------
class Blinker:
    """Flashes a pixel's existing color off/on, then either starts it falling
    (ENABLE_FALLING) or just leaves it lit in place (single blink, used while
    falling is disabled so the board can fill toward FULL_THRESHOLD)."""

    def __init__(self, x, y, color, now):
        self.x = x
        self.y = y
        self.color = color
        self.phase = 0
        self.last_phase = 3 if ENABLE_FALLING else 1
        self.next_time = now

    def advance(self, now):
        """Returns True when finished (caller removes it from the list)."""
        if self.phase % 2 == 0:
            set_pixel(self.x, self.y, 0x000000)
        else:
            set_pixel(self.x, self.y, self.color)
        if self.phase == self.last_phase:
            if ENABLE_FALLING:
                start_fall(self.x, self.y, self.color)
            else:
                cell_state[self.y][self.x] = LIT
            return True
        self.phase += 1
        self.next_time = now + BLINK_PERIOD
        return False


def advance_blinks(now):
    if not blinks:
        return
    keep = []
    for b in blinks:
        if now >= b.next_time and b.advance(now):
            continue
        keep.append(b)
    blinks[:] = keep


# --- full-board sweep: everything slides off together, grouped by column
# (or row, if gravity points sideways). Groups start falling one at a time,
# in random order, staggered by a short random delay each -----------------
def start_sweep():
    global sweep_active, sweep_dx, sweep_dy, sweep_ngroups, sweep_remaining
    global sweep_next_idx, sweep_next_start_time
    gc.collect()  # reclaim fragmented garbage before the sweep runs
    sweep_active = True
    # The sweep takes exclusive ownership of every non-off cell, including
    # ones mid-blink or mid-fall -- clearing these lists stops advance_blinks
    # / advance_falling from also touching the same cells this tick.
    blinks.clear()
    falling.clear()
    sweep_dx, sweep_dy = gravity_direction()
    sweep_ngroups = WIDTH if sweep_dy != 0 else HEIGHT
    # Random activation order: groups start falling one at a time in this
    # order, each staggered from the last by a short random delay (see
    # advance_sweep), rather than every group getting an independent random
    # start time up front -- that's what makes them visibly fall together,
    # just staggered, instead of arriving in a scattered wave.
    for k in range(sweep_ngroups):
        sweep_order[k] = k
    for i in range(sweep_ngroups - 1, 0, -1):
        j = rng.below(i + 1)
        sweep_order[i], sweep_order[j] = sweep_order[j], sweep_order[i]
    for k in range(sweep_ngroups):
        sweep_started[k] = False
        sweep_offset[k] = 0
    sweep_next_idx = 0
    sweep_next_start_time = time.monotonic()  # first group starts right away
    sweep_remaining = 0
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if cell_state[y][x] != OFF:
                cell_state[y][x] = ANIM
                sweep_remaining += 1
    print(
        "sweep: %d/%d (%.0f%%) lit, clearing %d groups"
        % (lit_count, WIDTH * HEIGHT, 100 * lit_count / (WIDTH * HEIGHT), sweep_ngroups)
    )


def advance_sweep(now):
    global sweep_active, lit_count, sweep_remaining, sweep_next_idx, sweep_next_start_time
    if not sweep_active:
        return
    # Kick off however many groups have come due since the last call (in the
    # random order chosen by start_sweep), each queued a short random gap
    # after the one before it.
    while sweep_next_idx < sweep_ngroups and now >= sweep_next_start_time:
        k = sweep_order[sweep_next_idx]
        sweep_started[k] = True
        sweep_group_start[k] = now
        sweep_offset[k] = 0
        sweep_next_idx += 1
        sweep_next_start_time = now + rng.jitter(SWEEP_STAGGER_MAX)
    any_due = False
    for k in range(sweep_ngroups):
        if not sweep_started[k]:
            sweep_due[k] = False
            continue
        due = now >= sweep_group_start[k] + sweep_offset[k] * FALL_STEP_PERIOD
        sweep_due[k] = due
        if due:
            any_due = True
            sweep_offset[k] += 1
    if not any_due:
        return
    # Walk the grid leading-edge-first so a cell moved this tick is never
    # re-read and moved again later in the same pass (see MAX_GROUPS comment
    # above -- this whole function uses no per-cell allocations).
    y_range = range(HEIGHT - 1, -1, -1) if sweep_dy > 0 else range(HEIGHT)
    x_range = range(WIDTH - 1, -1, -1) if sweep_dx > 0 else range(WIDTH)
    for y in y_range:
        for x in x_range:
            if cell_state[y][x] != ANIM:
                continue
            key = x if sweep_dy != 0 else y
            if not sweep_due[key]:
                continue
            color = cell_color[y][x]
            set_pixel(x, y, 0x000000)
            cell_state[y][x] = OFF
            nx, ny = x + sweep_dx, y + sweep_dy
            if 0 <= nx < WIDTH and 0 <= ny < HEIGHT:
                set_pixel(nx, ny, color)
                cell_color[ny][nx] = color
                cell_state[ny][nx] = ANIM
            else:
                sweep_remaining -= 1
    if sweep_remaining <= 0:
        sweep_active = False
        lit_count = 0
        print("sweep: done, board clear")


def on_tick():
    global lit_count
    x, y = rng.pixel()
    state = cell_state[y][x]
    if state == OFF:
        color = rng.color()
        cell_state[y][x] = LIT
        cell_color[y][x] = color
        set_pixel(x, y, color)
        lit_count += 1
        if VERBOSE:
            print("lit (%d,%d) #%06x  lit_count=%d" % (x, y, color, lit_count))
        check_full()
    elif state == LIT:
        cell_state[y][x] = ANIM
        if ENABLE_FALLING:
            lit_count -= 1
        if VERBOSE:
            print("blink (%d,%d)  lit_count=%d" % (x, y, lit_count))
        blinks.append(Blinker(x, y, cell_color[y][x], time.monotonic()))
    # else ANIM: already mid blink/fall, nothing to do this tick


# --- hue-frequency: drop a cycling-hue pixel into a random lane, stack it
# against the far wall (in whichever direction the accelerometer currently
# says is down), and let a completely full lane fall off on its own --------
def start_hue_clear(lane):
    hue_clearing[lane] = True
    hue_dropping[lane] = True  # keep blocking new drops while it empties
    hue_clear_next_time[lane] = time.monotonic()
    if VERBOSE:
        print("hue: lane %d full, falling off" % lane)


def advance_hue_falling(now):
    if not hue_falling:
        return
    dx, dy = hue_dx, hue_dy
    keep = []
    for particle in hue_falling:
        x, y, color, next_step = particle
        if now < next_step:
            keep.append(particle)
            continue
        nx, ny = x + dx, y + dy
        if not (0 <= nx < WIDTH and 0 <= ny < HEIGHT) or cell_state[ny][nx] != OFF:
            # blocked by a wall or another settled pixel -- this is where it
            # lands, so this is what must persist for the clear cascade to
            # later read the right color back out of the grid.
            cell_state[y][x] = LIT
            cell_color[y][x] = color
            lane = x if dy != 0 else y
            hue_dropping[lane] = False
            continue
        set_pixel(x, y, 0x000000)
        cell_state[y][x] = OFF
        set_pixel(nx, ny, color)
        cell_state[ny][nx] = ANIM
        particle[0], particle[1], particle[3] = nx, ny, now + FALL_STEP_PERIOD
        keep.append(particle)
    hue_falling[:] = keep


def advance_hue_clearing(now):
    for lane in range(MAX_GROUPS):
        if not hue_clearing[lane]:
            continue
        if now < hue_clear_next_time[lane]:
            continue
        hue_clear_next_time[lane] = now + FALL_STEP_PERIOD
        dx, dy = hue_dx, hue_dy
        moved_any = False
        # Leading-edge-first walk so a cell moved this pass isn't re-read
        # later in the same pass -- same trick as advance_sweep.
        if dy != 0:
            col = lane
            for y in range(HEIGHT - 1, -1, -1) if dy > 0 else range(HEIGHT):
                if cell_state[y][col] != LIT:
                    continue
                moved_any = True
                color = cell_color[y][col]
                set_pixel(col, y, 0x000000)
                cell_state[y][col] = OFF
                ny = y + dy
                if 0 <= ny < HEIGHT:
                    set_pixel(col, ny, color)
                    cell_color[ny][col] = color
                    cell_state[ny][col] = LIT
        else:
            row = lane
            for x in range(WIDTH - 1, -1, -1) if dx > 0 else range(WIDTH):
                if cell_state[row][x] != LIT:
                    continue
                moved_any = True
                color = cell_color[row][x]
                set_pixel(x, row, 0x000000)
                cell_state[row][x] = OFF
                nx = x + dx
                if 0 <= nx < WIDTH:
                    set_pixel(nx, row, color)
                    cell_color[row][nx] = color
                    cell_state[row][nx] = LIT
        if not moved_any:
            hue_clearing[lane] = False
            hue_dropping[lane] = False


def on_tick_hue():
    global hue_index
    dx, dy = hue_dx, hue_dy
    nlanes = WIDTH if dy != 0 else HEIGHT
    lane = rng.below(nlanes)
    if dy != 0:
        spawn_x, spawn_y = lane, (0 if dy > 0 else HEIGHT - 1)
    else:
        spawn_x, spawn_y = (0 if dx > 0 else WIDTH - 1), lane
    if hue_dropping[lane] or hue_clearing[lane]:
        return  # that lane's busy; a later tick will pick another
    if cell_state[spawn_y][spawn_x] != OFF:
        start_hue_clear(lane)  # stacked all the way to the spawn edge: full
        return
    color = HUE_COLORS[hue_index]
    hue_index = (hue_index + 1) % len(HUE_COLORS)
    hue_dropping[lane] = True
    set_pixel(spawn_x, spawn_y, color)
    cell_state[spawn_y][spawn_x] = ANIM
    cell_color[spawn_y][spawn_x] = color
    hue_falling.append([spawn_x, spawn_y, color, time.monotonic() + FALL_STEP_PERIOD])
    if VERBOSE:
        print("hue: drop lane=%d (%d,%d) #%06x" % (lane, spawn_x, spawn_y, color))


print("Trust M matrix: chip session open, starting.")
next_tick = time.monotonic()
next_status = time.monotonic()
while True:
    now = time.monotonic()
    poll_buttons()
    if mode_index == MODE_RANDOM_COLOR:
        advance_blinks(now)
        advance_falling(now)
        advance_sweep(now)
        if not sweep_active and now >= next_tick:
            on_tick()
            next_tick = now + DELAYS[delay_index]
    else:
        advance_hue_falling(now)
        advance_hue_clearing(now)
        if now >= next_tick:
            on_tick_hue()
            next_tick = now + DELAYS[delay_index]
    if now >= next_status:
        if mode_index == MODE_RANDOM_COLOR:
            print(
                "status: %d/%d lit (%.0f%%), free mem %d bytes"
                % (lit_count, WIDTH * HEIGHT, 100 * lit_count / (WIDTH * HEIGHT), gc.mem_free())
            )
        else:
            print("status: hue-frequency mode, free mem %d bytes" % gc.mem_free())
        next_status = now + STATUS_PERIOD
    display.refresh()
    time.sleep(0.01)
