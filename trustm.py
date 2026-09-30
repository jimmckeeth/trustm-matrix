# SPDX-License-Identifier: AGPL-3.0-or-later
"""CircuitPython driver for the Infineon OPTIGA Trust M (I2C addr 0x30).

Covers the IFX I2C data-link layer + APDU command layer for session
lifecycle, GetRandom, and the data-object commands (read/write/metadata,
transparently chained past one I2C frame). Ported from the physical/
data-link layer used by the Arduino and MicroPython host libraries (register
map 0x80 DATA / 0x82 I2C_STATE / 0x88 SOFT_RESET; FCTR = (frnr<<2)|acknr;
FCS = CRC-16 poly 0x8408, init 0) -- see Infineon's Solution Reference Manual,
chapter "IFX I2C Protocol", for the full spec if this needs extending.

The crypto commands (hash, sign/verify, key generation, ECDH, key
derivation, RSA encrypt/decrypt) live in the sibling module `trustm_crypto`,
which is built on top of this one and reaches into `_command`/`_tlv` here --
see that module's docstring for what's covered and what's deliberately not
(there's no Shielded Connection / SetObjectProtected support anywhere in
this driver: that needs the encrypted-session handshake described in
Infineon's Solution Reference Manual, which isn't available locally, so
implementing it would mean guessing at an undocumented crypto scheme).
See TRUSTM.md for full API documentation.
"""
import time

from adafruit_bus_device.i2c_device import I2CDevice

_ADDR = 0x30
_TRIES = 200
_GUARD_S = 0.0001

_REG_DATA = 0x80
_REG_STATE = 0x82
_REG_SOFT_RESET = 0x88

_STANDARD_AID = bytes.fromhex("D2760000044765" "6E41757468417070" "6C")

_CMD_OPEN_APPLICATION = 0x70
_CMD_CLOSE_APPLICATION = 0x71
_CMD_GET_DATA_OBJECT = 0x01
_CMD_SET_DATA_OBJECT = 0x02
_CMD_GET_RANDOM = 0x0C

# GetDataObject/SetDataObject param byte: which mode a call is making.
_PARAM_DATA = 0x00
_PARAM_ERASE_AND_WRITE = 0x40
_PARAM_METADATA = 0x01

# Chip status byte, GetDataObject: read past the object's actual stored
# length -- expected/normal when chaining a read to find an object's end,
# not a failure (see read_data()).
_STATUS_BOUNDARY = 0x08

# This driver doesn't negotiate an I2C frame size with the chip (the real
# host libraries do, via DATA_REG_LEN); this is a conservative single-frame
# payload ceiling confirmed empirically against this chip, comfortably under
# the datasheet's default 272-byte MAX_PACKET_SIZE. read_data/write_data
# chain automatically past this for objects that don't fit in one frame.
_MAX_CHUNK = 200

# Safety cap on a length=None read_data() call: this chip's objects are all
# well under this in practice (the biggest, a certificate slot, is ~1.7KB),
# so hitting this means the chip never signaled a boundary -- something's
# wrong, and the alternative is an unbounded read growing memory forever.
_MAX_UNBOUNDED_READ = 4096

# A handful of the chip's factory-provisioned object IDs, useful on their
# own (chip_uid) or as starting points for experimenting with read_data/
# write_data. See TRUSTM.md for the full table and what's NOT known (RSA
# cert/key slot pairing is Infineon's documented *intent*, not something
# this driver enforces -- any cert OID can be used with any key OID).
OID_UID = 0xE0C2  # coprocessor UID, read-only, fixed at the factory
OID_CERT_0 = 0xE0E0
OID_KEY_0 = 0xE0F0
OID_CERT_1 = 0xE0E1
OID_KEY_1 = 0xE0F1
OID_COUNTER_0 = 0xE120
OID_COUNTER_1 = 0xE121
OID_COUNTER_2 = 0xE122
OID_COUNTER_3 = 0xE123


def _crc16(data):
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c


def _tlv(tag, value):
    """One APDU parameter TLV: 1-byte tag, 2-byte big-endian length, value.

    Shared with trustm_crypto, which builds all of its command payloads
    out of these.
    """
    return bytes([tag]) + len(value).to_bytes(2, "big") + value


class TrustMError(OSError):
    """Raised for transport/protocol/chip failures (bad caller arguments
    raise plain ValueError instead -- see e.g. random()).

    `status` holds the raw chip status byte when the error came from a
    failed command (None for transport-level errors like a bad checksum
    or a timeout), so a caller that needs to tell an expected condition
    (e.g. "verify failed", "read past the end of this object") apart from
    a genuine failure can inspect it instead of string-matching args[0].
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class TrustM:
    """One session with the chip: soft reset, then OpenApplication."""

    def __init__(self, i2c):
        self._dev = I2CDevice(i2c, _ADDR, probe=False)
        self._frnr = 0
        self._acknr = 3
        self._soft_reset()
        self._open_application()

    # -- transport: single I2C transactions, retried while the chip NACKs
    # (asleep or busy -- Infineon's own host libraries retry up to 200 times
    # at 1ms intervals) --------------------------------------------------
    def _write(self, data):
        for _ in range(_TRIES):
            try:
                with self._dev as bus:
                    bus.write(data)
                time.sleep(_GUARD_S)
                return
            except OSError:
                time.sleep(0.001)
        raise TrustMError("write not acked")

    def _read(self, n):
        buf = bytearray(n)
        for _ in range(_TRIES):
            try:
                with self._dev as bus:
                    bus.readinto(buf)
                time.sleep(_GUARD_S)
                return buf
            except OSError:
                time.sleep(0.001)
        raise TrustMError("read not acked")

    def _reg(self, addr, n):
        self._write(bytes([addr]))
        return self._read(n)

    def _state(self):
        return self._reg(_REG_STATE, 4)

    def _soft_reset(self):
        self._write(bytes([_REG_SOFT_RESET, 0, 0]))
        time.sleep(0.025)

    def _wait_response(self, timeout=1.0):
        t0 = time.monotonic()
        while True:
            s = self._state()
            if s[0] & 0x40:  # RESP_RDY
                return (s[2] << 8) | s[3]
            if time.monotonic() - t0 > timeout:
                raise TrustMError("no response, state %s" % bytes(s).hex())
            time.sleep(0.005)

    def _read_data(self, timeout=1.0):
        n = self._wait_response(timeout)
        return self._reg(_REG_DATA, n)

    def _read_frame(self, timeout=1.0):
        # skip the chip's zero-length ack of our last write, if it comes first
        while True:
            f = self._read_data(timeout)
            if len(f) > 5 or f[1:3] != b"\x00\x00":
                return f

    # -- data-link layer: one frame per APDU, no chaining (payloads here
    # are always well under the negotiated frame size) -------------------
    def _send(self, apdu):
        body = (
            bytes([(self._frnr << 2) | self._acknr])
            + len(apdu + b"\x00").to_bytes(2, "big")
            + b"\x00"
            + apdu
        )
        frame = bytes([_REG_DATA]) + body + _crc16(body).to_bytes(2, "big")
        self._write(frame)
        self._frnr = (self._frnr + 1) & 3

    def _recv(self, timeout=1.0):
        f = self._read_frame(timeout)
        if _crc16(f[:-2]) != int.from_bytes(f[-2:], "big"):
            raise TrustMError("bad frame checksum")
        self._acknr = (f[0] >> 2) & 3
        ack = bytes([0x80 | self._acknr, 0, 0])
        self._write(bytes([_REG_DATA]) + ack + _crc16(ack).to_bytes(2, "big"))
        return f[4:-2]  # drop FCTR, LEN, PCTR and FCS

    def _command(self, cmd, param, data, timeout=1.0):
        # timeout matters for slow operations (RSA key generation can take
        # several seconds -- the 1.0s default that's plenty for everything
        # else isn't enough, confirmed on hardware: it raised "no response,
        # state 0x88800000" -- the BUSY bit set, chip still working, not an
        # actual protocol failure).
        self._send(bytes([cmd, param]) + len(data).to_bytes(2, "big") + data)
        r = self._recv(timeout)
        if r[0] != 0:
            raise TrustMError(
                "command 0x%02x failed, status 0x%02x" % (cmd, r[0]), status=r[0]
            )
        length = int.from_bytes(r[2:4], "big")
        return r[4 : 4 + length]

    def _open_application(self):
        self._command(_CMD_OPEN_APPLICATION, 0x00, _STANDARD_AID)

    def close(self):
        """Close the current application session (CloseApplication)."""
        self._command(_CMD_CLOSE_APPLICATION, 0x00, b"")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # Don't let a failure closing out the session on the way out mask
        # a real exception from the `with` block (or itself become one if
        # there isn't one -- the caller is done with the chip either way).
        try:
            self.close()
        except TrustMError:
            pass

    def random(self, n):
        """True random bytes from the chip's hardware RNG (GetRandom, 8..256 bytes)."""
        if not 8 <= n <= 256:
            raise ValueError("trustm: random length must be 8..256")
        return bytes(self._command(_CMD_GET_RANDOM, 0x00, n.to_bytes(2, "big")))

    # -- data objects: read/write/metadata, chained past one I2C frame for
    # objects bigger than _MAX_CHUNK (certificates, exported keys, ...) ---
    def _read_data_chunk(self, oid, offset, length):
        req = oid.to_bytes(2, "big") + offset.to_bytes(2, "big") + length.to_bytes(2, "big")
        return self._command(_CMD_GET_DATA_OBJECT, _PARAM_DATA, req)

    def _write_data_chunk(self, oid, offset, data, param):
        req = oid.to_bytes(2, "big") + offset.to_bytes(2, "big") + data
        self._command(_CMD_SET_DATA_OBJECT, param, req)

    def read_data(self, oid, offset=0, length=None, into=None):
        """Read a data object (GetDataObject), chaining past one frame.

        Reads `length` bytes if given, else reads until the chip reports
        the object's boundary (its actual stored length may be shorter
        than requested -- that's the normal way to discover it, not an
        error). Fills `into` (a bytearray, e.g. one already sized from
        read_metadata()) in place and returns the byte count if given,
        else returns a freshly grown `bytes` object.
        """
        buf = bytearray() if into is None else None
        pos = 0
        while length is None or pos < length:
            if length is None and pos >= _MAX_UNBOUNDED_READ:
                raise TrustMError("read_data: no boundary after %d bytes" % pos)
            want = _MAX_CHUNK if length is None else min(_MAX_CHUNK, length - pos)
            try:
                chunk = self._read_data_chunk(oid, offset + pos, want)
            except TrustMError as err:
                if err.status == _STATUS_BOUNDARY and pos > 0:
                    break  # ran off the end of the object -- done, not a fault
                raise
            if into is not None:
                into[pos : pos + len(chunk)] = chunk
            else:
                buf += chunk
            pos += len(chunk)
            if len(chunk) < want:
                break  # object ended exactly at this chunk
        return pos if into is not None else bytes(buf)

    def write_data(self, oid, data, offset=0, erase=False):
        """Write a data object (SetDataObject), chaining past one frame.

        `erase` erase-and-writes the object before the first chunk; every
        chunk after the first is always a plain write (matches how the
        chip's own erase-and-write semantics apply to the whole object,
        not each chunk). Any failure aborts immediately -- the object may
        be left partially written; there's no automatic retry/recovery.
        """
        param = _PARAM_ERASE_AND_WRITE if erase else _PARAM_DATA
        pos = 0
        while pos < len(data):
            chunk = data[pos : pos + _MAX_CHUNK]
            self._write_data_chunk(oid, offset + pos, chunk, param)
            pos += len(chunk)
            param = _PARAM_DATA

    def read_metadata(self, oid):
        """Read an object's metadata TLV blob (GetDataObject, metadata mode).

        Metadata objects are small and always fit in one frame -- no
        chaining needed. Returned as the raw TLV bytes (starts with tag
        0x20); this driver doesn't parse it into fields.
        """
        return bytes(
            self._command(_CMD_GET_DATA_OBJECT, _PARAM_METADATA, oid.to_bytes(2, "big"))
        )

    def write_metadata(self, oid, blob):
        """Write an object's metadata TLV blob (SetDataObject, metadata mode)."""
        self._command(_CMD_SET_DATA_OBJECT, _PARAM_METADATA, oid.to_bytes(2, "big") + blob)

    def chip_uid(self):
        """This chip's factory-fixed unique ID (OID_UID), as raw bytes.

        Read length-agnostic (via read_data's chaining) rather than
        assuming a fixed size -- sources disagree on the exact length.
        """
        return self.read_data(OID_UID)
