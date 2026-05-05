"""
app/encryption.py
-----------------
3DES encryption matching Pyro's Java implementation (EncryptionUtilsClientShared).

Algorithm  : Triple DES (DESede)
Mode       : ECB  — no IV required
Padding    : PKCS5 (8-byte DES block size)
Key derive : SHA-1(secret_key_string) -> 20 bytes + pad 4 zero bytes = 24 bytes
Output     : Base64 string WITHOUT trailing '=' padding

IMPORTANT — what gets encrypted vs what doesn't:
  - REQUEST bodies  : encrypt(json.dumps(body), secret_key)  → send as raw text
  - RESPONSE bodies : plain JSON returned by Pyro → NO decryption needed
  - CALLBACK bodies : plain JSON posted by Pyro  → NO decryption needed
"""

import base64
import hashlib

from Crypto.Cipher import DES3
from Crypto.Util.Padding import pad, unpad


def _derive_key(secret_key: str) -> bytes:
    """
    Mirrors Java:
        MessageDigest.getInstance("SHA-1").digest(secretKey.getBytes("utf-8"))
        Arrays.copyOf(digestOfPassword, 24)
    SHA-1 produces 20 bytes. copyOf to 24 pads with 4 zero bytes.
    """
    sha1 = hashlib.sha1(secret_key.encode("utf-8")).digest()  # 20 bytes
    return sha1 + b"\x00" * 4                                  # pad to 24 bytes


def encrypt(message: str, secret_key: str) -> str:
    """
    Encrypts a plain-text string (typically a JSON body) with 3DES-ECB.

    Args:
        message    : plain text to encrypt — pass json.dumps(body) for API calls
        secret_key : PYRO_SECRET_KEY from settings (raw string, not hex)

    Returns:
        Base64-encoded encrypted string WITHOUT trailing '=' — ready to POST
        as raw text body to any Pyro endpoint.
    """
    key = _derive_key(secret_key)
    cipher = DES3.new(key, DES3.MODE_ECB)
    padded = pad(message.encode("utf-8"), 8)   # PKCS5: 8-byte DES block
    return base64.b64encode(cipher.encrypt(padded)).decode("utf-8").rstrip("=")


def decrypt(encrypted_text: str, secret_key: str) -> str:
    """
    Decrypts a 3DES-ECB Base64-encoded ciphertext back to plain text.

    
    """
    key = _derive_key(secret_key)
    padded_b64 = encrypted_text + "=" * (-len(encrypted_text) % 4)
    encrypted_bytes = base64.b64decode(padded_b64)
    cipher = DES3.new(key, DES3.MODE_ECB)
    return unpad(cipher.decrypt(encrypted_bytes), 8).decode("utf-8")