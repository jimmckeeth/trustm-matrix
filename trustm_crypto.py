# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crypto commands for the Infineon OPTIGA Trust M, built on trustm.TrustM.

Covers: CalcHash (one-shot SHA-256), GenKeyPair (ECC/RSA), CalcSign,
VerifySign, CalcSSec (ECDH), DeriveKey (TLS-PRF-SHA256), EncryptAsym/
DecryptAsym (RSA). See TRUSTM.md for full API documentation, including the
raw-encoding caveats (bare BIT STRING public keys, r/s-only signatures)
that matter if you're feeding results to another crypto library.

Deliberately NOT covered, and not planned:
  - The Shielded Connection (encrypted session, pre-shared secret OID
    0xE140) and SetObjectProtected -- the handshake protocol needs
    Infineon's Solution Reference Manual, which isn't available locally.
    Every command here uses the chip's default unprotected channel, which
    is sufficient for everything below on this chip's factory config.
  - On-chip session-secret OIDs (0xE100-0xE103) for CalcSSec/DeriveKey/
    EncryptAsym/DecryptAsym "never export the secret to the host" flows --
    the reference host library manages these with an async allocator this
    synchronous driver has no equivalent of. Every method here always
    exports its result to the host.
  - Streaming CalcHash for inputs bigger than one I2C frame -- one-shot
    covers every hash use case this project has needed so far.
  - Symmetric AES/HMAC -- this is a V1-generation chip; those are V3-only.

This module reaches into trustm._command and trustm._tlv, which is a
deliberate, documented crack in trustm.py's otherwise-private internals --
see that module's docstring.
"""
from trustm import TrustMError, _tlv

_CMD_CALC_HASH = 0x30
_CMD_GEN_KEY_PAIR = 0x38
_CMD_CALC_SIGN = 0x31
_CMD_VERIFY_SIGN = 0x32
_CMD_CALC_SSEC = 0x33
_CMD_DERIVE_KEY = 0x34
_CMD_ENCRYPT_ASYM = 0x1E
_CMD_DECRYPT_ASYM = 0x1F

_PARAM_HASH_SHA256 = 0xE2
_PARAM_ALGO_ECDSA = 0x11  # CalcSign/VerifySign scheme: ECDSA FIPS186-3, no hash
_PARAM_ALGO_ECDH = 0x01  # CalcSSec scheme: ECDH key agreement
_PARAM_ALGO_TLS_PRF = 0x01  # DeriveKey scheme: TLS PRF SHA-256
_PARAM_ALGO_RSA_PKCS1 = 0x11  # EncryptAsym/DecryptAsym scheme: RSAES-PKCS1-v1.5

# GenKeyPair param byte: which key type/curve to generate.
CURVE_P256 = 0x03
CURVE_P384 = 0x04
RSA_1024 = 0x41
RSA_2048 = 0x42

# GenKeyPair key-usage TLV value: what the generated key may be used for.
USAGE_AUTH = 0x01
USAGE_ENCRYPT = 0x02
USAGE_SIGN = 0x10
USAGE_KEY_AGREEMENT = 0x20

# CalcHash. The chip has no "cold" one-shot: even a single small hash
# needs a START call to hand back a context, which must be echoed back
# in the FINAL call alongside the data -- confirmed on real hardware
# (the reference library's own "one-shot" HASH command does the same
# two round trips internally, it's just not exposed to its caller).
_TAG_HASH_START = 0x00
_TAG_HASH_DIGEST_OUT = 0x01
_TAG_HASH_FINAL = 0x03
_TAG_HASH_INTERMEDIATE = 0x06
_TAG_HASH_CONTX_OUT = 0x07

# GenKeyPair
_TAG_GEN_PRIVATE_KEY_OID = 0x01
_TAG_GEN_KEY_USAGE = 0x02
_TAG_GEN_EXPORT_KEY = 0x07
_TAG_GEN_PUBLIC_KEY = 0x02  # response tag (distinct namespace from request)
_TAG_GEN_PRIVATE_KEY = 0x01  # response tag, export mode only

# CalcSign / VerifySign
_TAG_SIGN_DIGEST = 0x01
_TAG_SIGN_OID = 0x03
_TAG_VERIFY_SIGNATURE = 0x02
_TAG_VERIFY_PUB_KEY_OID = 0x04
_TAG_VERIFY_ALGO_ID = 0x05
_TAG_VERIFY_PUBLIC_KEY = 0x06

# Chip status byte, VerifySign: signature didn't verify -- expected/normal
# outcome for a bad signature, not a failure (see verify()). Confirmed on
# real hardware to be 0xFF, not the 0x2C some documentation suggests --
# 0xFF looks like it may be an overloaded/generic failure code on this
# chip/firmware (CalcHash returns it too, for an unrelated missing-context
# error), so there's some risk this mapping also swallows a genuinely
# malformed verify() request as a false "didn't verify" instead of
# surfacing it. Worth keeping in mind if verify() ever returns False when
# you're confident the signature should be valid.
_STATUS_VERIFY_FAILED = 0xFF

# CalcSSec
_TAG_SSEC_PRIVATE_KEY_OID = 0x01
_TAG_SSEC_ALGO_ID = 0x05
_TAG_SSEC_PUBLIC_KEY = 0x06
_TAG_SSEC_EXPORT = 0x07

# DeriveKey
_TAG_DERIVE_SEC_OID = 0x01
_TAG_DERIVE_DATA = 0x02
_TAG_DERIVE_KEY_LEN = 0x03
_TAG_DERIVE_EXPORT = 0x07
_DERIVE_MIN_LENGTH = 16  # chip enforces this floor regardless of what's asked for

# EncryptAsym / DecryptAsym
_TAG_CIPHER_MESSAGE = 0x61
_TAG_CIPHER_PUB_KEY_OID = 0x04
_TAG_CIPHER_ALGO_ID = 0x05
_TAG_CIPHER_PUBLIC_KEY = 0x06
_TAG_CIPHER_DECRYPT_KEY_OID = 0x03


def _find_tlv(blob, tag):
    """First TLV matching `tag` in a flat (non-nested) TLV blob, or None."""
    pos = 0
    while pos + 3 <= len(blob):
        t = blob[pos]
        length = int.from_bytes(blob[pos + 1 : pos + 3], "big")
        value = blob[pos + 3 : pos + 3 + length]
        if t == tag:
            return value
        pos += 3 + length
    return None


def to_der_signature(raw):
    """Wrap CalcSign's raw `r||s` (two DER INTEGERs, no outer SEQUENCE)
    output in a standard DER SEQUENCE, for callers feeding it to a library
    that expects one. Not applied automatically by sign() -- see its
    docstring for why."""
    return bytes([0x30, len(raw)]) + bytes(raw)


class TrustMCrypto:
    """Crypto commands for a chip session opened by trustm.TrustM.

    Doesn't open its own session -- construct a TrustM first and pass it
    in; this class only adds commands on top.
    """

    def __init__(self, chip):
        self._chip = chip

    def sha256(self, data):
        """SHA-256 digest of `data` (CalcHash). `data` must fit in one I2C
        frame; see trustm._MAX_CHUNK. Two round trips under the hood --
        START to get a context, then FINAL with the data and that context
        -- since the chip won't accept a FINAL with no prior START, even
        for a single small hash. Returns 32 bytes.
        """
        req = _tlv(_TAG_HASH_START, b"") + _tlv(_TAG_HASH_CONTX_OUT, b"")
        resp = self._chip._command(_CMD_CALC_HASH, _PARAM_HASH_SHA256, req)
        context = _find_tlv(resp, _TAG_HASH_INTERMEDIATE)
        req = _tlv(_TAG_HASH_FINAL, bytes(data)) + _tlv(_TAG_HASH_INTERMEDIATE, bytes(context))
        resp = self._chip._command(_CMD_CALC_HASH, _PARAM_HASH_SHA256, req)
        return bytes(_find_tlv(resp, _TAG_HASH_DIGEST_OUT))

    def generate_keypair(self, private_key_oid, curve, usage, export_private=False):
        """Generate a keypair (GenKeyPair) into `private_key_oid` (ECC:
        CURVE_P256/CURVE_P384, or RSA_1024/RSA_2048 despite the parameter
        name -- it's really "key type"). `usage` is one of the USAGE_*
        constants (or bitwise-or'd together).

        Returns (public_key, private_key_or_None). Both are the chip's raw
        encodings, not standard ones: the public key is a bare DER BIT
        STRING (03 <len> 00 04 <X><Y> for ECC) with no AlgorithmIdentifier
        -- the curve isn't self-describing, you already know it since you
        asked for it. The private key (export_private=True only) is a bare
        DER OCTET STRING. Neither loads directly into most crypto
        libraries' standard key-import functions without re-wrapping.
        """
        if export_private:
            req = _tlv(_TAG_GEN_EXPORT_KEY, b"")
        else:
            req = _tlv(_TAG_GEN_PRIVATE_KEY_OID, private_key_oid.to_bytes(2, "big"))
            req += _tlv(_TAG_GEN_KEY_USAGE, bytes([usage]))
        # RSA key generation is slow -- confirmed on hardware that the
        # default 1s response timeout isn't enough (chip reports BUSY,
        # not a real failure). ECC is fast enough for the default, but a
        # little slack costs nothing.
        timeout = 20.0 if curve in (RSA_1024, RSA_2048) else 2.0
        resp = self._chip._command(_CMD_GEN_KEY_PAIR, curve, req, timeout=timeout)
        public_key = bytes(_find_tlv(resp, _TAG_GEN_PUBLIC_KEY))
        private_key = _find_tlv(resp, _TAG_GEN_PRIVATE_KEY)
        return public_key, (bytes(private_key) if private_key is not None else None)

    def sign(self, digest, private_key_oid):
        """ECDSA-sign `digest` with the private key at `private_key_oid`
        (CalcSign). Returns the chip's raw response: two DER INTEGERs
        (r, s) concatenated, with NO outer SEQUENCE -- most libraries
        expect a full DER signature. Wrap with to_der_signature() first if
        you need that; left as an explicit opt-in rather than done
        automatically, since not every caller wants the extra bytes.
        """
        req = _tlv(_TAG_SIGN_DIGEST, bytes(digest))
        req += _tlv(_TAG_SIGN_OID, private_key_oid.to_bytes(2, "big"))
        return bytes(self._chip._command(_CMD_CALC_SIGN, _PARAM_ALGO_ECDSA, req))

    def verify(self, digest, signature, public_key_oid=None, public_key=None, curve=None):
        """Verify an ECDSA signature on-chip (VerifySign) against either a
        public-key/cert OID, or an explicit (curve, public_key) pair in
        the chip's raw encoding (see generate_keypair()). `signature` is
        the raw r/s form sign() returns (not a full DER signature).

        Returns True/False -- a failed verification (chip status 0x2C) is
        a normal outcome, not an exception. Any other failure still raises
        TrustMError.
        """
        req = _tlv(_TAG_SIGN_DIGEST, bytes(digest))
        req += _tlv(_TAG_VERIFY_SIGNATURE, bytes(signature))
        if public_key_oid is not None:
            req += _tlv(_TAG_VERIFY_PUB_KEY_OID, public_key_oid.to_bytes(2, "big"))
        else:
            req += _tlv(_TAG_VERIFY_ALGO_ID, bytes([curve]))
            req += _tlv(_TAG_VERIFY_PUBLIC_KEY, bytes(public_key))
        try:
            self._chip._command(_CMD_VERIFY_SIGN, _PARAM_ALGO_ECDSA, req)
        except TrustMError as err:
            if err.status == _STATUS_VERIFY_FAILED:
                return False
            raise
        return True

    def ecdh(self, private_key_oid, peer_public_key, curve):
        """ECDH shared secret (CalcSSec) between the private key at
        `private_key_oid` and `peer_public_key` (the chip's raw public-key
        encoding, matching curve). Always exports the secret to the host
        -- see module docstring re: session-OID "never export" flows.
        Returns the raw shared secret bytes.
        """
        req = _tlv(_TAG_SSEC_PRIVATE_KEY_OID, private_key_oid.to_bytes(2, "big"))
        req += _tlv(_TAG_SSEC_ALGO_ID, bytes([curve]))
        req += _tlv(_TAG_SSEC_PUBLIC_KEY, bytes(peer_public_key))
        req += _tlv(_TAG_SSEC_EXPORT, b"")
        return bytes(self._chip._command(_CMD_CALC_SSEC, _PARAM_ALGO_ECDH, req))

    def derive_key(self, shared_secret_oid, label, seed, length=32):
        """Derive `length` bytes via TLS-PRF-SHA256 (DeriveKey) from the
        secret at `shared_secret_oid`, using `label + seed` as the PRF's
        seed input. The chip enforces a 16-byte minimum regardless of what
        's requested. Always exports to the host. Returns the derived
        bytes.

        NOT CONFIRMED WORKING on real hardware -- request TLV order was
        fixed to match source exactly, but it still fails (chip status
        0xFF) when `shared_secret_oid` points at a plain arbitrary data
        object this driver wrote to directly with write_data(). This
        driver has no way to produce the kind of input DeriveKey may
        actually expect here: either a real CalcSSec-computed secret
        stored to an internal session OID (a code path this driver
        deliberately doesn't implement -- see module docstring), or an
        object with some specific "shared secret" type marking beyond
        plain arbitrary data. Treat this method as implemented-from-source
        but unverified until that's sorted out.
        """
        length = max(length, _DERIVE_MIN_LENGTH)
        # Order matters here -- confirmed on hardware. SEC_OID, then
        # KEY_LEN, then DERIVATION_DATA; sending DERIVATION_DATA before
        # KEY_LEN (an equally reasonable-looking tag order) gets rejected.
        req = _tlv(_TAG_DERIVE_SEC_OID, shared_secret_oid.to_bytes(2, "big"))
        req += _tlv(_TAG_DERIVE_KEY_LEN, length.to_bytes(2, "big"))
        req += _tlv(_TAG_DERIVE_DATA, bytes(label) + bytes(seed))
        req += _tlv(_TAG_DERIVE_EXPORT, b"")
        return bytes(self._chip._command(_CMD_DERIVE_KEY, _PARAM_ALGO_TLS_PRF, req))

    def rsa_encrypt(self, message, public_key_oid=None, public_key=None, key_type=None):
        """RSA-PKCS1v1.5 encrypt `message` (EncryptAsym) with either a
        public-key/cert OID, or an explicit (key_type, public_key) pair in
        the chip's raw encoding. Returns the ciphertext bytes.
        """
        req = _tlv(_TAG_CIPHER_MESSAGE, bytes(message))
        if public_key_oid is not None:
            req += _tlv(_TAG_CIPHER_PUB_KEY_OID, public_key_oid.to_bytes(2, "big"))
        else:
            req += _tlv(_TAG_CIPHER_ALGO_ID, bytes([key_type]))
            req += _tlv(_TAG_CIPHER_PUBLIC_KEY, bytes(public_key))
        # Source (optiga_cmd_encrypt_asym_handler) skips a fixed tag+length
        # header without checking the tag value, then copies the length it
        # read -- consistent with the response reusing the request's own
        # message tag, which is what _find_tlv looks for here. Not yet
        # exercised against real hardware, unlike the rest of this module.
        resp = self._chip._command(_CMD_ENCRYPT_ASYM, _PARAM_ALGO_RSA_PKCS1, req)
        return bytes(_find_tlv(resp, _TAG_CIPHER_MESSAGE) or resp)

    def rsa_decrypt(self, ciphertext, private_key_oid):
        """RSA-PKCS1v1.5 decrypt `ciphertext` (DecryptAsym) with the
        private key at `private_key_oid`. Always exports the plaintext to
        the host. Returns the plaintext bytes.
        """
        req = _tlv(_TAG_CIPHER_MESSAGE, bytes(ciphertext))
        req += _tlv(_TAG_CIPHER_DECRYPT_KEY_OID, private_key_oid.to_bytes(2, "big"))
        # Same caveat as rsa_encrypt() above re: the response tag.
        resp = self._chip._command(_CMD_DECRYPT_ASYM, _PARAM_ALGO_RSA_PKCS1, req)
        return bytes(_find_tlv(resp, _TAG_CIPHER_MESSAGE) or resp)
