import base64
import hmac
import struct
import time

"""
To add TOTP functionality without adding external dependencies.
Code taken from:
https://github.com/susam/mintotp
"""


def hotp(key: str, counter: int, digits: int = 6, digest: str = "sha1") -> str:
    # Secrets are often copied with grouping spaces or dashes; base32 decoding
    # would reject those, so strip them first.
    key = key.replace(" ", "").replace("-", "")
    key_bytes = base64.b32decode(key.upper() + "=" * ((8 - len(key)) % 8))
    counter_bytes = struct.pack(">Q", counter)
    mac = hmac.new(key_bytes, counter_bytes, digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">L", mac[offset: offset + 4])[0] & 0x7FFFFFFF
    return str(binary)[-digits:].zfill(digits)


def totp(key: str, time_step: int = 30, digits: int = 6, digest: str = "sha1") -> str:
    return hotp(key, int(time.time() / time_step), digits, digest)
