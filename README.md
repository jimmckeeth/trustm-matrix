# Trust M Matrix

*What does real randomness look like?*

A random-pixel light show on a 64x32 RGB LED matrix, driven entirely by the
hardware true-random-number generator (TRNG) inside an [Infineon OPTIGA
Trust M](https://www.infineon.com/part/OPTIGA-TRUST-M-MTR) secure element — 
not a software pseudo-random generator. Real random in real time. 

## Display Variations

The board can show two different visualizations of the same underlying
randomness. Hold **UP** or **DOWN** for one second to rotate between them
(a quick tap instead changes the tick speed — see [Usage](#usage)).

### random-color

<img width="640" height="480" alt="random-colors" src="https://github.com/user-attachments/assets/879da6ac-2dec-4deb-b6f9-1e4ec456882e" />

Every tick, a genuinely random pixel is chosen (position and color both
come straight from the chip's RNG). If the pixel was off, it lights up in
a random color. If it was already lit, it blinks. Once enough of the board
is lit, the whole thing empties in one synchronized column/row sweep, with
gravity direction read live from the board's onboard accelerometer — so it
animates the right way whichever way the board is held or mounted.

### hue-frequency

A calmer, single-hue-at-a-time variation. Each tick, a random lane
(column, or row if the board is tilted sideways) is chosen and a pixel of
the current color drops into it from the "top" (as determined live by the
onboard accelerometer), stacking on whatever's already there. The color
then advances to the next shade in a 17-color cycle running through every
Tailwind CSS "600" shade from red to rose. When a lane fills up
completely, the whole lane falls off the far edge — pixels keeping their
original colors as they fall — while new pixels keep dropping into other
lanes without missing a beat.

## Hardware / Bill of Materials

| Part | Notes |
|------|-------|
| [Adafruit Matrix Portal M4](https://www.adafruit.com/product/4745) | ATSAMD51J19 Cortex-M4, runs CircuitPython, has a built-in HUB75 matrix connector, an onboard LIS3DH accelerometer, and a STEMMA QT (I2C) port |
| [64x32 RGB LED matrix panel](https://www.adafruit.com/product/2278) | HUB75-style, plugs directly into the Matrix Portal M4's onboard connector |
| [Infineon OPTIGA Trust M (SLS32AIA) breakout](https://www.adafruit.com/product/4351) | Needs a STEMMA QT / Qwiic (JST-SH 4-pin) connector; this is what supplies the hardware TRNG |
| [STEMMA QT / Qwiic cable](https://www.adafruit.com/product/4399) | Connects the Trust M breakout to the Matrix Portal M4's STEMMA QT port |
| 5V power supply | Sized for the LED panel — a 64x32 panel can draw several amps at full brightness, more than USB alone typically provides |
| USB-C cable | For programming the Matrix Portal M4 (and power, if the panel's own supply is separate) |

That is what I used, but you should be able to adapt it to any size LED matrix. Just adjust the matrix size in the code.

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

Written for CircuitPython 10.x, but later versions may also work. 

1. Download [CircuitPython for the Matrix Portal M4](https://circuitpython.org/board/matrixportal_m4/). 
2. Install on the [CircuitPython on the Matrix Portal M4](https://learn.adafruit.com/adafruit-matrixportal-m4/install-circuitpython).
3. Copy `code.py` and `trustm.py` from this repo to the root of the
   `CIRCUITPY` drive that appears when the board is plugged in.
4. Download the
   bundle matching your CircuitPython version from
   [circuitpython.org/libraries](https://circuitpython.org/libraries).
5. Copy these libraries into a `lib/` folder on `CIRCUITPY`:
   - `adafruit_bus_device/` (the whole folder)
   - `adafruit_lis3dh.mpy`
   - `adafruit_matrixportal/__init__.py` and `adafruit_matrixportal/matrix.mpy`
     only — the rest of that package (networking helpers, etc.) isn't used
     here and doesn't need to be copied.
6. Power up the board. CircuitPython runs `code.py` automatically; no
   further setup is needed.

## Usage

- The board starts with the display all black in `random-color` mode and
  begins lighting random pixels immediately.
- Tapping the **UP** and **DOWN** buttons steps through a list of tick
  speeds (`DELAYS` in `code.py`), from very fast (a few milliseconds) to
  slow (several seconds).
- Holding **UP** or **DOWN** for one second rotates to the next display
  variation (see [Display Variations](#display-variations)) instead of
  changing speed.
- In `random-color` mode, once enough of the board is lit
  (`FULL_THRESHOLD` in `code.py`, a fraction of the total pixel count),
  the whole board sweeps itself clear in one synchronized animation and
  starts filling again.
- `ENABLE_FALLING` in `code.py` switches between two modes, in
  `random-color`, for what happens when an already-lit pixel is picked
  again:
  - `True`: it blinks twice, then falls off the display in the direction
    of gravity (read from the onboard accelerometer) — like sand draining
    off the board a pixel at a time.
  - `False`: it just blinks once and stays lit — useful for reliably
    filling the board up so you can watch the full sweep animation.

## How the randomness works

Pixel positions, colors, and animation timing are all derived from raw
bytes pulled from the Trust M's `GetRandom` command over its I2C protocol
(implemented from scratch in `trustm.py`). Positions are extracted by masking random
bytes against the (power-of-two) width and height, which is exactly
uniform with no modulo bias.

## The Trust M driver

`trustm.py` (plus `trustm_crypto.py`) is a complete, from-scratch
CircuitPython driver for the OPTIGA Trust M — not just a `random()` call.
This project only exercises `GetRandom`, but the same driver also does
session lifecycle management, arbitrary data-object read/write/metadata,
SHA-256 hashing, ECC/RSA key generation, ECDSA sign/verify, ECDH, TLS-PRF
key derivation, and RSA encrypt/decrypt, all verified against real
hardware. There is no existing CircuitPython library for talking to the
Trust M's I2C protocol directly (existing libraries assume a bridge
microcontroller), so this was built from the protocol description in
Infineon's Solution Reference Manual and cross-checked against a
third-party MicroPython driver.

See **[TRUSTM.md](TRUSTM.md)** for the full API reference, constants, and
known limitations.

## License

 [GNU Affero General Public License (AGPL)](LICENSE.md)
