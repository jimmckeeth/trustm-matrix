# SPDX-License-Identifier: AGPL-3.0-or-later
"""Random visualizations for the 64x32 RGB matrix, seeded entirely by the
OPTIGA Trust M's hardware TRNG.

This file only does orchestration: hardware setup, button handling, picking
which visualization is active, and driving the main loop. Each visualization
lives in its own module (see MODES) and is documented there:

  random-color   viz_random_color.py    truly-random pixels, then a column sweep
  hue-frequency  viz_hue_frequency.py   stacking drops cycling through 17 hues
  brownian       viz_brownian.py        random walkers with fading trails
  lava           viz_lava.py            evolving Perlin noise, drifting palette
  voronoi        viz_voronoi.py         drifting Voronoi cells
  beacons        viz_beacons.py         five public randomness beacons (needs WiFi)

Buttons: tapping UP/DOWN steps the shared tick `delay` through DELAYS
(UP = slower, DOWN = faster; applies to every visualization); holding either
button for LONG_PRESS_SECONDS rotates to the next/previous visualization
instead (UP = next, DOWN = previous).

Visualization modules are imported when selected and unloaded when left, so
only one is resident in RAM at a time -- the Matrix Portal M4 only has
192KB. See vizcore.py for the interface each one implements.
"""

import gc
import sys
import time

import adafruit_lis3dh
import board
import digitalio

import trustm
import vizcore

DELAYS = (0.001, 0.01, 0.1, 0.5, 1, 2, 5)
DELAY_INDEX_DEFAULT = 0
LONG_PRESS_SECONDS = 1.0  # hold UP/DOWN this long to switch visualization
STATUS_PERIOD = 5  # seconds between periodic status prints

# (module name, class name), in rotation order. The first one runs on boot.
MODES = (
    ("viz_random_color", "RandomColor"),
    ("viz_hue_frequency", "HueFrequency"),
    ("viz_brownian", "Brownian"),
    ("viz_lava", "Lava"),
    ("viz_voronoi", "Voronoi"),
    ("viz_beacons", "Beacons"),
)

# --- hardware: shared STEMMA I2C bus (Trust M TRNG + onboard LIS3DH) -------
canvas = vizcore.Canvas()
i2c = board.STEMMA_I2C()
chip = trustm.TrustM(i2c)
accel = adafruit_lis3dh.LIS3DH_I2C(i2c, address=0x19)
env = vizcore.Env(canvas, vizcore.TrueRandom(chip), vizcore.Gravity(accel))

# --- buttons: no external pull-ups on this board, pressed = low ------------
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

# --- active visualization ---------------------------------------------------
mode_index = 0
viz = None


def load_mode(index):
    """Unload the current visualization (module included, to free its RAM),
    blank the panel, then import and construct the one at MODES[index]."""
    global viz, mode_index
    if viz is not None:
        close = getattr(viz, "close", None)
        if close:
            close()  # optional: release pins etc. so the mode can be re-entered
        viz = None
        sys.modules.pop(MODES[mode_index][0], None)
    mode_index = index
    canvas.clear()
    gc.collect()
    module_name, class_name = MODES[index]
    module = __import__(module_name)
    viz = getattr(module, class_name)(env)
    print("mode ->", viz.NAME)


def switch_mode(direction):
    load_mode((mode_index + direction) % len(MODES))


def poll_buttons():
    """Short tap (press then release before LONG_PRESS_SECONDS) steps the
    shared delay; holding past LONG_PRESS_SECONDS switches visualization
    instead and suppresses the delay-step on release. Actions fire on release
    rather than press (needed to tell a tap from a hold)."""
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


load_mode(0)
print("Trust M matrix: chip session open, starting.")
next_status = time.monotonic()
while True:
    now = time.monotonic()
    poll_buttons()
    viz.update(now, DELAYS[delay_index])
    if now >= next_status:
        print("status: %s, free mem %d bytes" % (viz.status(), gc.mem_free()))
        next_status = now + STATUS_PERIOD
    canvas.refresh()
    time.sleep(0.01)
