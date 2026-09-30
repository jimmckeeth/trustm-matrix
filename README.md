# Trust M Matrix

A random-pixel light show on a 64x32 RGB LED matrix, driven entirely by the
hardware true-random-number generator (TRNG) inside an Infineon OPTIGA
Trust M secure element — not a software pseudo-random generator.

Every tick, a genuinely random pixel is chosen (position and color both
come straight from the chip's RNG). If the pixel was off, it lights up in
a random color. If it was already lit, it blinks. Once enough of the board
is lit, the whole thing empties in one synchronized column/row sweep, with
gravity direction read live from the board's onboard accelerometer — so it
animates the right way whichever way the board is held or mounted.

## Hardware / Bill of Materials

| Qty | Part | Notes |
|-----|------|-------|
| 1 | [Adafruit Matrix Portal M4](https://www.adafruit.com/product/4745) | ATSAMD51J19 Cortex-M4, runs CircuitPython, has a built-in HUB75 matrix connector, an onboard LIS3DH accelerometer, and a STEMMA QT (I2C) port |
| 1 | [64x32 RGB LED matrix panel](https://www.adafruit.com/product/2278) | HUB75-style, plugs directly into the Matrix Portal M4's onboard connector |
| 1 | [Infineon OPTIGA Trust M (SLS32AIA) breakout](https://www.adafruit.com/product/4351) | Needs a STEMMA QT / Qwiic (JST-SH 4-pin) connector; this is what supplies the hardware TRNG |
| 1 | [STEMMA QT / Qwiic cable](https://www.adafruit.com/product/4399) | Connects the Trust M breakout to the Matrix Portal M4's STEMMA QT port |
| 1 | 5V power supply | Sized for the LED panel — a 64x32 panel can draw several amps at full brightness, more than USB alone typically provides |
| 1 | USB-C cable | For programming the Matrix Portal M4 (and power, if the panel's own supply is separate) |

### Wiring

- The LED matrix panel plugs straight into the Matrix Portal M4's built-in
  HUB75 connector — no separate wiring needed.
- The Trust M breakout connects over the STEMMA QT cable to the Matrix
  Portal M4's STEMMA QT port. This shares the same I2C bus as the board's
  onboard LIS3DH accelerometer (used for gravity direction) — the Trust M
  sits at I2C address `0x30`, the accelerometer at `0x19`, so there's no
  conflict.
- No other wiring is required; the accelerometer is built into the board.

## Software Setup

1. Install CircuitPython 10.x on the Matrix Portal M4 — see
   [circuitpython.org/board/matrixportal_m4](https://circuitpython.org/board/matrixportal_m4/)
   for the firmware image and installation instructions.
2. Copy `code.py` and `trustm.py` from this repo to the root of the
   `CIRCUITPY` drive that appears when the board is plugged in.
3. Copy these libraries into a `lib/` folder on `CIRCUITPY`. Download the
   bundle matching your CircuitPython version from
   [circuitpython.org/libraries](https://circuitpython.org/libraries) and
   pull out just:
   - `adafruit_bus_device/` (the whole folder)
   - `adafruit_lis3dh.mpy`
   - `adafruit_matrixportal/__init__.py` and `adafruit_matrixportal/matrix.mpy`
     only — the rest of that package (networking helpers, etc.) isn't used
     here and doesn't need to be copied.
4. Power up the board. CircuitPython runs `code.py` automatically; no
   further setup is needed.

## Usage

- The board starts with the display all black and begins lighting random
  pixels immediately.
- The **UP** and **DOWN** buttons step through a list of tick speeds
  (`DELAYS` in `code.py`), from very fast (a few milliseconds) to slow
  (several seconds).
- Once enough of the board is lit (`FULL_THRESHOLD` in `code.py`, a
  fraction of the total pixel count), the whole board sweeps itself clear
  in one synchronized animation and starts filling again.
- `ENABLE_FALLING` in `code.py` switches between two modes for what
  happens when an already-lit pixel is picked again:
  - `True`: it blinks twice, then falls off the display in the direction
    of gravity (read from the onboard accelerometer) — like sand draining
    off the board a pixel at a time.
  - `False`: it just blinks once and stays lit — useful for reliably
    filling the board up so you can watch the full sweep animation.

## How the randomness works

Pixel positions, colors, and animation timing are all derived from raw
bytes pulled from the Trust M's `GetRandom` command over its I2C protocol
(implemented from scratch in `trustm.py`, since no existing CircuitPython
library talks to this chip). Positions are extracted by masking random
bytes against the (power-of-two) width and height, which is exactly
uniform with no modulo bias.

## License

See `LICENSE.md`.
