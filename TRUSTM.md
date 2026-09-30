# trustm.py / trustm_crypto.py — OPTIGA Trust M driver reference

A from-scratch CircuitPython driver for the Infineon OPTIGA Trust M
(SLS32AIA) secure element, talking directly to the chip's I2C interface —
no bridge microcontroller, no vendored Infineon library. This document
covers the driver as a standalone library, independent of any particular
project that happens to use it (the matrix-visualization app in this repo
only uses `TrustM.random()` — see `README.md`/`CLAUDE.md` for that).

## What this driver does and doesn't do

Implements: session lifecycle (open/close), true-random generation,
arbitrary data-object read/write/metadata (chained transparently past one
I2C frame), SHA-256 hashing, ECC/RSA key generation, ECDSA sign/verify,
ECDH, TLS-PRF key derivation, RSA encrypt/decrypt.

Deliberately does **not** implement:
- **The Shielded Connection** (encrypted I2C session, using a pre-shared
  secret at OID `0xE140`) or `SetObjectProtected`. The handshake/session-key
  protocol for this is described in Infineon's Solution Reference Manual,
  which isn't available in this project — implementing it without that
  spec would mean guessing at an undocumented crypto scheme, so it's out.
  Every command in this driver uses the chip's default unprotected
  channel, which this chip's factory configuration allows for everything
  below (reads, key use, signing, etc. — only *writes* to a handful of
  protected objects need the Shielded Connection, and this driver doesn't
  touch those objects).
- **On-chip session-secret OIDs** (`0xE100`–`0xE103`) for "the ECDH/derived
  secret never leaves the chip" flows. Every method here always exports
  its result to the host.
- **Streaming CalcHash** for inputs larger than one I2C frame — only
  one-shot hashing is implemented.
- **Symmetric AES/HMAC** — this chip generation (V1) doesn't have them at
  all; they're V3-only silicon features.

## Quick start

```python
import board
import trustm
import trustm_crypto

i2c = board.STEMMA_I2C()

with trustm.TrustM(i2c) as chip:
    print(chip.random(16))
    print(chip.chip_uid())

    crypto = trustm_crypto.TrustMCrypto(chip)
    digest = crypto.sha256(b"hello")
```

`TrustM(i2c)` opens a session immediately (soft reset + OpenApplication) —
there's no separate `open()` call. Use it as a context manager to close
the session automatically, or call `chip.close()` yourself.

## API reference — `trustm.py`

### `TrustM(i2c)`
Opens a session: soft reset, then OpenApplication. Raises `TrustMError` if
the chip doesn't respond.

### `chip.close()`
Sends CloseApplication. Raises `TrustMError` on failure. Also available as
`__exit__` via `with TrustM(i2c) as chip: ...`, which swallows a failed
close so it doesn't mask an exception from inside the `with` block.

### `chip.random(n)`
True-random bytes from the chip's hardware RNG (GetRandom). `n` must be
8–256; raises `ValueError` outside that range, `TrustMError` on chip/
protocol failure. Returns `bytes`.

```python
chip.random(32)  # -> 32 random bytes
```

### `chip.read_data(oid, offset=0, length=None, into=None)`
Reads a data object (GetDataObject), transparently chaining multiple I2C
frames for objects bigger than one frame (~200 bytes, see `_MAX_CHUNK`).

- `length=None` (default): reads until the chip signals the object's
  actual end, so you don't need to know the size in advance. Capped at
  4096 bytes total as a safety net against a misbehaving chip (raises
  `TrustMError` if hit — this chip's real objects are all well under that).
- `length=<n>`: reads exactly `n` bytes (or raises if the object is
  shorter and the chip signals boundary early).
- `into=<bytearray>`: fills it in place instead of allocating a new
  buffer, and returns the byte count instead of a `bytes` object — use
  this when you already know the size (e.g. from `read_metadata`) and
  want to avoid an extra allocation on this memory-constrained board.

```python
chip.read_data(trustm.OID_CERT_0)              # -> bytes, whatever's there
chip.read_data(trustm.OID_UID)                 # -> bytes, this chip's UID
buf = bytearray(64)
n = chip.read_data(some_oid, into=buf)         # buf[:n] now holds the data
```

### `chip.write_data(oid, data, offset=0, erase=False)`
Writes a data object (SetDataObject), chaining past one frame the same
way. `erase=True` erase-and-writes before the first chunk (every following
chunk is always a plain write — matches the chip's own semantics: erase
applies to the whole object, not each chunk). Raises `TrustMError`
immediately on any failure; the object may be left partially written if
that happens partway through a multi-chunk write — no automatic retry.

### `chip.read_metadata(oid)` / `chip.write_metadata(oid, blob)`
Read/write an object's metadata (lifecycle state, size, access
conditions, ...) as a raw TLV blob (starts with tag `0x20`). This driver
doesn't parse the TLV into fields — you get the bytes as the chip sent
them. Metadata objects are small and never need chaining.

### `chip.chip_uid()`
Shorthand for `chip.read_data(trustm.OID_UID)` — this chip's factory-fixed
unique ID. Read length-agnostic rather than assuming a fixed size (see
Known Limitations).

## API reference — `trustm_crypto.py`

Every method below takes a `TrustM` instance at construction and adds
commands on top — it doesn't open its own session.

```python
crypto = trustm_crypto.TrustMCrypto(chip)
```

### `crypto.sha256(data)`
SHA-256 digest (CalcHash). `data` must fit in one I2C frame. Two round
trips under the hood — the chip requires a `START` call to hand back a
context before it will accept a `FINAL` with the actual data, even for a
single small hash; there's no truly "cold" one-shot path, despite what a
naive reading of the command's tag list suggests. This is handled for you.
Returns 32 bytes.

### `crypto.generate_keypair(private_key_oid, curve, usage, export_private=False)`
Generates a keypair on-chip (GenKeyPair). `curve` is one of
`CURVE_P256`, `CURVE_P384`, `RSA_1024`, `RSA_2048`. `usage` is one of
`USAGE_AUTH`, `USAGE_ENCRYPT`, `USAGE_SIGN`, `USAGE_KEY_AGREEMENT` (or
bitwise-or'd together).

Returns `(public_key, private_key_or_None)`. **Both are the chip's raw
encodings, not standard ones**:
- Public key: a bare DER `BIT STRING` (`03 <len> 00 04 <X><Y>` for ECC,
  or a standard PKCS#1 `RSAPublicKey` SEQUENCE for RSA) with **no
  `AlgorithmIdentifier`** — the curve/key type isn't self-describing in
  the response; you already know it since you asked for it.
- Private key (only present if `export_private=True`): a bare DER
  `OCTET STRING`.

**RSA key generation is slow** — confirmed on hardware to exceed this
driver's normal 1-second command-response timeout (it isn't a failure,
the chip reports itself as still busy). This method waits up to 20
seconds for RSA, 2 for everything else; if 20s isn't enough on your
hardware, that's a constant (`timeout` inside `generate_keypair`) you'd
need to raise.

Neither loads directly into most crypto libraries' standard key-import
functions without re-wrapping into a full `SubjectPublicKeyInfo`/PKCS#8
structure first.

### `crypto.sign(digest, private_key_oid)`
ECDSA-signs `digest` (CalcSign). **Returns two DER `INTEGER`s (r, s)
concatenated, with no outer `SEQUENCE`** — not a complete DER signature.
Use `trustm_crypto.to_der_signature(raw)` to wrap it if a library needs a
full DER signature; not done automatically, since not every caller wants
the extra bytes.

### `crypto.verify(digest, signature, public_key_oid=None, public_key=None, curve=None)`
Verifies an ECDSA signature entirely on-chip (VerifySign). Either pass
`public_key_oid`, or an explicit `(curve, public_key)` pair using the raw
encoding `generate_keypair` returns. `signature` is the raw r/s form
`sign()` returns (not a full DER signature).

**`public_key_oid` must be a certificate OID (e.g. `OID_CERT_0`), not a
private-key-storage OID** (e.g. `OID_KEY_0`) — confirmed on hardware: the
chip parses the public key out of a certificate at that OID, it doesn't
treat it as "the public half of the key stored here." If you generated a
keypair without a matching certificate written to its paired cert slot
(the common case when just testing), use the explicit `(curve,
public_key)` form with the `public_key` `generate_keypair` returned you
instead — that's the path confirmed working end-to-end in this project.

Returns `True`/`False` — a failed verification (chip status `0xFF`,
confirmed on real hardware) is a normal result, not an exception. Any
other failure still raises `TrustMError`. Caveat: `0xFF` looks like it may
be an overloaded/generic failure code on this chip rather than one
specifically meaning "didn't verify" (see Known Limitations) — a `False`
result is good evidence of a bad signature, but isn't an absolute
guarantee the request itself was otherwise well-formed.

### `crypto.ecdh(private_key_oid, peer_public_key, curve)`
ECDH shared secret (CalcSSec) between the key at `private_key_oid` and
`peer_public_key` (raw chip encoding). Always exports the secret to the
host. Returns raw bytes. **Confirmed on hardware**: matched byte-for-byte
against an independent desktop `cryptography` ECDH computation of the
same exchange.

### `crypto.derive_key(shared_secret_oid, label, seed, length=32)`
TLS-PRF-SHA256 key derivation (DeriveKey) from a secret at
`shared_secret_oid`. `label + seed` form the PRF's seed input. The chip
enforces a 16-byte minimum output regardless of what's requested — this
method raises that floor silently if you ask for less. Always exports to
the host.

**NOT confirmed working.** Fails with chip status `0xFF` when
`shared_secret_oid` points at a plain arbitrary data object written via
`write_data()` — even after fixing the request TLV order to match source
exactly (see Known Limitations). It likely needs either a real
`CalcSSec`-computed secret stored to an internal session OID (a code path
this driver doesn't implement) or an object with a specific "shared
secret" type marking. Treat this as implemented-from-source, not
verified.

### `crypto.rsa_encrypt(message, public_key_oid=None, public_key=None, key_type=None)` / `crypto.rsa_decrypt(ciphertext, private_key_oid)`
RSA-PKCS1v1.5 encrypt/decrypt (EncryptAsym/DecryptAsym). Decrypt always
exports the plaintext to the host. **Confirmed on hardware**: a full
generate-keypair → encrypt → decrypt round trip with an RSA-1024 key
recovered the exact original plaintext.

### `trustm_crypto.to_der_signature(raw)`
Wraps `sign()`'s raw r/s output in a standard DER `SEQUENCE`. See `sign()`.

## Constants reference

### Object IDs (`trustm.py`)

| Constant | OID | Purpose |
|---|---|---|
| `OID_UID` | `0xE0C2` | Coprocessor UID, read-only, fixed at the factory |
| `OID_CERT_0` | `0xE0E0` | Certificate slot 0 |
| `OID_KEY_0` | `0xE0F0` | Private key paired with `OID_CERT_0` |
| `OID_CERT_1` | `0xE0E1` | Certificate slot 1 |
| `OID_KEY_1` | `0xE0F1` | Private key paired with `OID_CERT_1` |
| `OID_COUNTER_0`..`OID_COUNTER_3` | `0xE120`–`0xE123` | Monotonic up-counters (4 slots, 8 bytes each: 4-byte value + 4-byte threshold) |

Not exposed as constants (no local documentation confirms them precisely
enough to hardcode): the RSA cert/key slot pair (Configuration Guide
suggests `0xE0E2`/`0xE0FC`, but a real hardware object dump shows generic-
looking spare slots there instead — see Known Limitations), and the
`0xF1D0`/`0xF1E0`/`0xF1E1`-range general-purpose "scratch" data objects
some references mention as safe to experiment with.

### Curves / key types, usage bits, algorithm IDs (`trustm_crypto.py`)

| Constant | Value | Meaning |
|---|---|---|
| `CURVE_P256` | `0x03` | NIST P-256, for `generate_keypair`/`ecdh`/`verify` |
| `CURVE_P384` | `0x04` | NIST P-384 |
| `RSA_1024` | `0x41` | RSA 1024-bit |
| `RSA_2048` | `0x42` | RSA 2048-bit |
| `USAGE_AUTH` | `0x01` | Key usage: authentication |
| `USAGE_ENCRYPT` | `0x02` | Key usage: encryption |
| `USAGE_SIGN` | `0x10` | Key usage: signing |
| `USAGE_KEY_AGREEMENT` | `0x20` | Key usage: ECDH |

### Status codes worth knowing

| Status | Meaning | Where it's handled specially |
|---|---|---|
| `0x00` | Success | Everywhere |
| `0x08` | Data object boundary exceeded | `read_data()` — expected/normal when chaining without a known length, not raised as long as at least one chunk already came back |
| `0xFF` | Overloaded/generic failure -- confirmed meanings include "signature verification failed" (VerifySign) and "no context provided" (CalcHash without a prior START) | `verify()` — returned as `False`, not raised, for this command specifically |

Any other nonzero status raises `TrustMError` with that status attached
as `.status`.

## Known limitations

- **UID length**: local documentation disagreed on whether `chip_uid()`
  should be 27 or 37 bytes before this was implemented; confirmed on real
  hardware to be 27. `read_data()`'s length-agnostic chaining sidesteps
  needing to know this in advance regardless — whatever the chip reports
  as the boundary is what you get.
- **`_MAX_CHUNK` (200 bytes)**: this driver doesn't negotiate an I2C frame
  size with the chip like the reference host libraries do (via the
  `DATA_REG_LEN` register). 200 is a conservative value known to work for
  single-chunk reads (confirmed via `chip_uid()`); the multi-chunk chaining
  path itself hasn't yet been exercised against an object bigger than one
  chunk on real hardware. It could likely go higher than 200 (the
  datasheet's default frame ceiling is 272
  bytes) but hasn't been pushed further.
- **No streaming hash**: `sha256()` is one-shot only; inputs bigger than
  one frame aren't supported. The streaming protocol (context export/
  import via separate TLV tags) is a known, documented extension point if
  a future need for it comes up, just not implemented.
- **`verify()`'s `public_key_oid` needs a certificate OID, not a
  private-key OID** — confirmed on hardware (see its docstring/reference
  entry above). Easy to get this wrong if you assume the OID you generated
  a keypair into is also where its public half can be "looked up."
- **This chip's APDU parser appears order-sensitive**, not a generic
  tag-search TLV parser -- confirmed twice on hardware: `CalcHash` rejects
  a cold `FINAL` (needs `START` first, see `sha256()`), and `DeriveKey`
  rejected `DERIVATION_DATA` before `KEY_LEN` even though both are
  present and well-formed, only succeeding once reordered to match
  source exactly. Every TLV order in this module was re-checked against
  source after that second discovery; if you extend this module, verify
  the order against `optiga_cmd.c` rather than assuming any order that
  includes all the right tags will work.
- **`derive_key` still fails (status `0xFF`) even with the corrected
  order** -- see its reference entry above and the module docstring. Not
  resolved.
- **No on-chip session secrets**: every `ecdh`/`derive_key`/RSA method
  always exports its result to host memory. There's no way with this
  driver to compute an ECDH secret and feed it straight into `derive_key`
  without it passing through the host in between -- and this may be
  directly related to why `derive_key` doesn't work yet, see above.
- **RSA cert/key slot pairing** (`0xE0E2`/`0xE0FC` per Infineon's
  Configuration Guide) is a documented *convention*, not something this
  driver enforces or even hardcodes as constants — a real hardware object
  dump consulted during development showed generic-looking spare slots in
  that range rather than anything RSA-specific, so treat any such pairing
  as your own choice of which OIDs to use together, not a driver
  guarantee.
- **Default command timeout (1 second) is too short for slow chip
  operations.** Discovered via RSA key generation (which can take several
  seconds) reporting "no response, state 0x88800000" -- the BUSY bit set,
  not a real failure. `_command()`/`_recv()`/`_read_frame()`/`_read_data()`
  all take an optional `timeout` (seconds) now; `generate_keypair()` uses
  20s for RSA, 2s otherwise. If you add a method for some other
  known-slow operation, check whether it needs a longer timeout too
  rather than assuming 1s is always enough.
- **Verified against real hardware, end to end**: `sha256`,
  `generate_keypair` → `sign` → `verify` (including a deliberately
  tampered signature correctly failing), `ecdh` (matched an independent
  desktop `cryptography` computation of the same exchange byte-for-byte),
  and `generate_keypair` → `rsa_encrypt` → `rsa_decrypt` (RSA-1024,
  recovered the exact original plaintext). Not yet verified:
  `derive_key` (see above), and RSA-2048 specifically (only RSA-1024 was
  tested, for speed). See the project's `CLAUDE.md` for the full history.
