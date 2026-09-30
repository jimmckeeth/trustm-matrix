See `README.md` for the hardware/BOM and setup instructions. This file is
implementation notes for working on the code, not user-facing docs.

## Status: implemented and verified on hardware

`code.py` (main program) and `trustm.py` (chip driver) are deployed to the
board's CIRCUITPY drive along with the libraries listed in `README.md`.
Verified live over the serial console: the Trust M session opens,
`GetRandom` calls succeed, pixels light up in random colors, and the
blink/fall/sweep animation logic runs repeatedly with no exceptions.

**There is no existing CircuitPython library for talking to the Trust M's
I2C protocol** (the Arduino/host libraries that exist assume a bridge
microcontroller, which this board doesn't need or use — it talks to the
chip directly over its own STEMMA I2C bus). `trustm.py` is a from-scratch
CircuitPython port of the IFX I2C data-link + APDU layer, based on the
protocol described in Infineon's OPTIGA Trust M Solution Reference Manual
("IFX I2C Protocol" chapter) and cross-checked against a third-party
MicroPython driver (`clawdbotatg/clawd-trust-m`, `firmware/trustm.py`).
Register map: I2C slave address `0x30`, registers `0x80` DATA / `0x82`
I2C_STATE / `0x88` SOFT_RESET; data-link framing uses `FCTR = (frnr<<2) |
acknr` and a CRC-16 (poly `0x8408`, init `0`) frame checksum. It only
originally implemented only what `random()` needed: soft reset,
OpenApplication (standard AID), and GetRandom (command `0x0C`). Since
expanded into a much fuller driver — see "Full driver expansion" below
and `TRUSTM.md` for complete API documentation.

**Design decision — the fill/blink/fall interpretation.** The original spec
here said pixels "blink twice before changing", and separately that once
the field is all black it should "animate all the pixels falling off the
bottom". Read completely literally these can't both be true — once
everything is black there's nothing left to animate. The implementation
resolves this by treating "changing" a lit pixel as *removing* it, and doing
that removal *by* falling off the display (blink twice, then slide toward
gravity and disappear at the edge) — so falling is the continuous mechanism
by which the field returns to black, not a separate one-shot event
triggered by an all-black check. This uses the accelerometer on every
removal rather than rarely. (The full-board synchronized sweep, added
later and described below, ended up covering the "big wipe" half of this
directly.)

**Accelerometer axis mapping is unverified against the physical mounting**
— `gravity_direction()` in `code.py` picks whichever of ax/ay from the
onboard LIS3DH (address `0x19`) is larger in magnitude and uses its sign.
Whether that matches "down" on the actual panel depends on how the board is
mounted; `ACCEL_INVERT_X` / `ACCEL_INVERT_Y` at the top of `code.py` are
there to flip it once observed running. Couldn't verify this visually in
this environment (no camera on the matrix) — only confirmed via serial log
that the read and the fall loop don't error.

**Button mapping** (`code.py`, `poll_buttons`): `board.BUTTON_UP` increases
the delay (slower), `board.BUTTON_DOWN` decreases it (faster), stepping
through `DELAYS` (currently `(0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10)` seconds,
widened for fast on-hardware testing; default index 0). Swap the `+1`/`-1`
in `poll_buttons` if the opposite feels more natural.

**Update: non-blocking animation rewrite.** Testing at fast delays exposed
three problems with the original blink-then-fall design, all fixed in the
current `code.py`:

1. *Falling froze while new pixels were being added.* `blink_then_fall` used
   to be a blocking function (`time.sleep()` across both blink phases),
   called synchronously from the tick handler, so it stalled the whole main
   loop -- including `advance_falling()` -- for ~0.5s per blink. Replaced
   with a `Blinker` class exposing a non-blocking `.advance(now)` step,
   tracked in a `blinks` list and driven every iteration by
   `advance_blinks(now)`, alongside `advance_falling(now)`. Ticking,
   blinking, and falling now all progress concurrently with no blocking
   sleeps in the hot path.
2. *A pixel would blink and then just vanish instead of falling.* Root
   cause: the old code blanked the pixel right before handing it to
   `start_fall`, and if the very first fall step was blocked by a
   neighboring lit cell, the code rescheduled and left it in the `falling`
   list without redrawing anything -- so it sat invisible forever. Fixed by
   (a) `Blinker.advance()` leaving the pixel lit on its final phase instead
   of blanking it, and (b) a blocked particle in `advance_falling` now
   settles back into an ordinary `LIT` cell at its current position
   (visible, and eligible to be revisited/retried on a later tick) rather
   than disappearing into limbo.
3. *Spontaneous resets after running a while, independent of power source.*
   Not conclusively root-caused, but two contributing factors were
   addressed: the vanish bug above could leave orphaned entries floating
   with no path back to a stable state, and the per-tick debug `print()`
   calls over USB CDC were firing on every single tick at the fast test
   delays. Per-tick prints are now gated behind `VERBOSE = False` (flip on
   only for debugging) -- only button presses and sweep start/end print
   by default. A ~5-8 minute soak test after this change showed no resets,
   tracebacks, or serial disconnects, but this wasn't run long enough to
   fully confirm the original issue (which took longer to reproduce) is
   gone -- worth continued observation.

**New feature: full-board synchronized sweep.** When `lit_count` reaches
`FULL_THRESHOLD` (fraction of the board's 2048 cells, checked in
`check_full()` after every increment), normal ticking pauses (`on_tick()`
is skipped while `sweep_active`) and `start_sweep()` fires: cells are
grouped by column (or row, if gravity is currently pointing sideways) and
each group gets an independent random start delay up to `SWEEP_JITTER_MAX`
(drawn from the Trust M's TRNG via `rng.jitter()`, for thematic
consistency), then `advance_sweep()` steps every group one cell per
`FALL_STEP_PERIOD` until all members are off-display, at which point
`lit_count` resets to 0 and normal ticking resumes. This satisfies the
request as-specified (columns, not individual pixels, fall together with
slight random timing) and its explicit fallback ("pause adding pixels
until the animation finishes") -- but the pause is scoped only to this
one-shot full-board event; individual per-pixel falls, when enabled, still
run concurrently with ticking per point 1 above.

**`ENABLE_FALLING` toggle.** Currently `False`: revisiting a lit pixel just
blinks it once and leaves it lit, so nothing removes cells between ticks
and the board reliably climbs to `FULL_THRESHOLD` (set to `0.75`) to show
the sweep animation on demand, without waiting on the long tail of the
coupon-collector problem a literal 100% threshold would require. Set back
to `True` to restore continuous individual falling on revisit (in which
case a lower `FULL_THRESHOLD` may rarely or never be reached, since
falling is what removes cells).

**Display**: uses `adafruit_matrixportal.matrix.Matrix` (bit_depth=6) rather
than configuring `rgbmatrix.RGBMatrix` by hand — it already knows this
board's `MTX_*` pins. Every one of the 2048 cells gets its own dedicated
`displayio.Palette` index (bitmap value == `y*64+x`, fixed at setup and
never changed), so any two pixels can hold independent random 24-bit colors
simultaneously with no palette-size limit — color changes are just
`palette[i] = color`.

**Random extraction**: pixel x/y come straight from masking TRNG bytes with
`WIDTH-1`/`HEIGHT-1` (both powers of two, so no modulo bias); colors are 3
raw TRNG bytes with pure black (0,0,0) nudged to avoid colliding with the
"off" state. `TrueRandom` in `code.py` batches `chip.random(64)` calls
rather than hitting the chip for every single draw.

Known CircuitPython gotcha hit and fixed during testing: `bytearray` slices
can't be deleted with `del buf[:n]` on this CircuitPython build — rebuild
via `buf = buf[n:]` instead (see `TrueRandom._fill`).

The `CIRCUITPY` drive doesn't always auto-mount on every OS/desktop
environment — see `README.md` for how to get files onto it.

## Full driver expansion: `trustm_crypto.py` + `trustm.py` additions

`trustm.py` gained session lifecycle (`close()`, context-manager support)
and data-object read/write/metadata (`read_data`/`write_data`/
`read_metadata`/`write_metadata`/`chip_uid`, transparently chained past
one I2C frame). `trustm_crypto.py` (new file) adds a `TrustMCrypto` class
for hashing, key generation, sign/verify, ECDH, key derivation, and RSA
encrypt/decrypt. Full API reference, constants, and known limitations are
in `TRUSTM.md` — this section is just the implementation history.

**Verified on real hardware**: `chip_uid()`/`read_metadata()`; `sha256()`;
a full `generate_keypair()` → `sign()` → `verify()` round trip including a
deliberately tampered signature correctly failing; `ecdh()` (matched an
independent desktop `cryptography` computation of the same exchange
byte-for-byte); a full `generate_keypair()` → `rsa_encrypt()` →
`rsa_decrypt()` round trip (RSA-1024) recovering the exact plaintext.
**Not verified**: `derive_key()` (fails with chip status `0xFF` even
after fixing its request TLV order to match source — see below and
`TRUSTM.md`), multi-chunk `read_data`/`write_data` chaining (only
single-chunk reads exercised so far), RSA-2048 specifically.

Test material left on the chip: slot 1 (`OID_KEY_1`/`0xE0F1`) has a test
P-256 key (usage last set to `KEY_AGREEMENT`, for the ECDH test) — this is
scratch, not the factory identity (that's slot 0, `0xE0F0`/`0xE0E0`,
confirmed via metadata to be a real locked P-256 auth key + populated
cert, and deliberately never touched). `0xE0FC` has a test RSA-1024
keypair. `0xF1D0` (a general-purpose scratch data object) had a test
32-byte value written to it for the `derive_key` investigation.

**Three real protocol bugs were found only by testing against real
hardware** — none of these were things the source-code research caught:

1. **`CalcHash` has no "cold" one-shot.** Sending `FINAL` directly with
   the message (skipping `START`) reliably failed with chip status
   `0xFF`, even though this seemed like valid usage reading the tag list.
   The chip requires `START` (requesting a context via `CONTX_OUT`) even
   for a single small hash; the returned context must be echoed back in
   the `FINAL` call. `sha256()` now does both round trips internally.
2. **`VerifySign`'s actual "didn't verify" status is `0xFF`, not the
   `0x2C`** that some documentation suggested. Confirmed by generating a
   keypair, signing, verifying (succeeds), then flipping a signature byte
   and verifying again (fails with `0xFF`). `0xFF` also being CalcHash's
   "missing context" status suggests it may be an overloaded/generic
   failure code on this chip rather than meaning one specific thing.
3. **This chip's APDU parser is order-sensitive**, not a generic
   tag-search TLV parser. `DeriveKey` was built with `DERIVATION_DATA`
   before `KEY_LEN` (an equally plausible-looking order) and failed;
   reordering to match source exactly (`SEC_OID`, `KEY_LEN`,
   `DERIVATION_DATA`) was necessary, though ultimately still insufficient
   to get `derive_key` working (see above). Every other command's TLV
   order was re-checked against source after this was found, and all
   matched already (`GenKeyPair`, `CalcSSec`, `EncryptAsym`/`DecryptAsym`).

A fourth, non-protocol bug: the default 1-second command-response
timeout wasn't enough for RSA key generation (the chip reports itself
BUSY, not failed) — `_command()` and friends now take an optional
`timeout` in seconds, and `generate_keypair()` uses 20s for RSA.

Also discovered along the way: `chip_uid()` is confirmed 27 bytes (not
37, which some earlier documentation suggested); slot 0
(`OID_KEY_0`/`OID_CERT_0`) is confirmed to hold this chip's real
factory-provisioned identity (locked P-256 key, populated certificate)
via metadata inspection *before* any write was attempted against it —
worth doing that check again before writing to any new OID on this chip.