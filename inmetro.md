# INMETRO beacon: TLS failure and the custom ESP32 firmware workaround

`viz_beacons.py` fetches five randomness beacons over HTTPS. Four work with the
stock ESP32 WiFi firmware. The INMETRO one
(`https://beacon.inmetro.gov.br/beacon/2.1/pulse/last`) does not:

```
beacon inmetro failed: BrokenPipeError('Expected 01 but got 00',)
```

## Cause

INMETRO's server sends only its leaf certificate (`*.inmetro.gov.br`) and **not
the intermediate CA** that issued it:

```
leaf:          CN=*.inmetro.gov.br
issuer:        CN=RNP ICPEdu GR46 OV TLS CA 2025        <- intermediate, NOT sent by the server
its issuer:    CN=GlobalSign Root R46                    <- root
```

A client can only verify the chain if it already has the intermediate. Desktop
`curl` fails the same way
(`unable to get local issuer certificate`); browsers cope because they fetch
missing intermediates from the leaf's "CA Issuers" (AIA) URL, which TLS stacks
like mbedTLS on the ESP32 don't do.

On the Matrix Portal the TLS stack lives on the ESP32 co-processor (the "NINA"
firmware, not CircuitPython). The ESP32 reports a failed `socket_open`, which
adafruit_esp32spi surfaces as `BrokenPipeError: Expected 01 but got 00`. That
error is generic: any TLS failure looks like this, so check with
`openssl s_client` from a desktop before blaming the board.

## What does not work

Tested on the board with NINA firmware 3.3.0 and read in the firmware source
(`main/CommandHandler.cpp`):

| Idea | Result |
| --- | --- |
| Install the intermediate CA from CircuitPython | No such command. adafruit_esp32spi only has `set_certificate` / `set_private_key`, which set a **client** identity. The firmware's TLS mode loads only its built-in bundle. |
| Turn verification off | No insecure mode. Setting a client cert + key makes the firmware skip loading the CA bundle, and then **every** HTTPS connection fails (nist and drand too). The setting also persists until the ESP32 resets. |
| Plain HTTP | `beacon.inmetro.gov.br` doesn't answer on port 80; NIST redirects to HTTPS. |

(An old ESP32 firmware, 1.2.2, was also a problem: it failed HTTPS to *all*
modern hosts. Updating to 3.3.0 fixed the other four beacons.)

## Workaround: add the intermediate to the firmware's certificate bundle

The firmware verifies against ESP-IDF's certificate bundle, and ESP-IDF can
append extra certificates to it at build time
(`CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE`). A certificate in the bundle acts
as a trust anchor even if it is an intermediate, so adding
`RNP ICPEdu GR46 OV TLS CA 2025` lets the chain verify while **verification
stays fully on**.

`inmetro.sh` reproduces the whole build:

1. Fetches the intermediate from the AIA URL in INMETRO's own leaf certificate.
2. Checks with `openssl verify` that it really issued the leaf, and aborts if not.
3. Clones `adafruit/nina-fw` at tag `3.3.0` (with submodules).
4. Adds the certificate and the two `sdkconfig.defaults` lines.
5. Builds `NINA_ADAFRUIT-esp32` in the `espressif/idf:v5.5` container (the
   version nina-fw 3.3.0 documents), using podman or docker.

```
./inmetro.sh            # builds into ./nina-build/ (override: WORKDIR=...)
```

The result is `nina-build/nina-fw/NINA_ADAFRUIT-esp32-3.3.0.bin` (the version is appended to the name), a combined image
flashed at address `0x0`. It needs several GB for the container image, a few
GB for the build tree, and 10-20 minutes the first time.

## Flashing the ESP32 from the Matrix Portal

The ESP32 has no USB of its own; flash it through the SAMD51 by making
CircuitPython a USB-to-UART bridge and holding the ESP32 in its ROM bootloader.
Everything below was used successfully on this board.

1. Back up what's on CIRCUITPY (at least `code.py`; the repo has the rest).
2. Copy these two files to the drive, then hard-reset the board
   (`import microcontroller; microcontroller.reset()` in the REPL) so
   `boot.py` takes effect. A second serial port (`/dev/ttyACM1`) appears.

   `boot.py`:
   ```python
   import usb_cdc
   usb_cdc.enable(console=True, data=True)
   ```

   `code.py` (temporary bridge):
   ```python
   import board, busio, digitalio, time, usb_cdc
   cs = digitalio.DigitalInOut(board.ESP_CS); cs.switch_to_output(True)
   gpio0 = digitalio.DigitalInOut(board.ESP_GPIO0); gpio0.switch_to_output(False)
   rst = digitalio.DigitalInOut(board.ESP_RESET); rst.switch_to_output(False)
   time.sleep(0.1)
   # Open the UART BEFORE releasing reset. Opening it afterwards lost the
   # ESP32's replies and esptool never synced.
   uart = busio.UART(board.ESP_TX, board.ESP_RX, baudrate=115200,
                     timeout=0, receiver_buffer_size=2048)
   rst.value = True          # GPIO0 low during reset => ROM download mode
   time.sleep(0.6)
   data = usb_cdc.data
   data.timeout = 0
   while True:
       n = data.in_waiting
       if n:
           uart.write(data.read(n))
       b = uart.read(256)
       if b:
           data.write(b)
   ```
3. Flash. The board can't toggle reset from DTR/RTS, so tell esptool not to
   reset, and stay at 115200 without the stub (the bridge doesn't follow baud
   changes). About 2 minutes for 1.3 MB:
   ```
   esptool --port /dev/ttyACM1 --baud 115200 --before no-reset --after no-reset \
           --no-stub write-flash 0x0 NINA_ADAFRUIT-esp32-3.3.0.bin
   ```
4. Delete `boot.py` from the drive, run `./deploy.sh` to restore the real
   `code.py`, and hard-reset the board. The ESP32 restarts on every `code.py`
   run, so there is nothing else to undo.

Check the result from the REPL: `esp.firmware_version` should print `3.3.0`,
and the serial log should stop showing `beacon inmetro failed`.

## Caveats

- **Expiry.** The intermediate is valid until 2030-11-19 and INMETRO's leaf
  until 2027-04-10. If INMETRO reissues from a different intermediate, rerun
  `inmetro.sh`, which always fetches the current one from the live leaf.
- **Hand-rolled firmware.** This image is stock nina-fw 3.3.0 plus one
  certificate. Future upstream firmware needs the script rerun.
- **Trust.** Adding an intermediate to the bundle trusts anything that CA signs
  (it is constrained to `pathlen:0`, i.e. it can only sign end-entity certs).
  The script verifies the downloaded certificate against the live leaf before
  using it, but the AIA download itself is plain HTTP; if that matters, compare
  the certificate's fingerprint with RNP's published one.
- **Fallback.** If you'd rather not run custom firmware, leave INMETRO's region
  red; the other four beacons are unaffected.
