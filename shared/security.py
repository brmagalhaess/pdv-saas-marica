"""Separate offline operator PINs; never reuse or transfer a web password hash."""
import hashlib
import hmac
import secrets

ROUNDS = 310000

def hash_pin(pin):
    if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 6 <= len(pin) <= 12:
        raise ValueError("PIN precisa de 6 a 12 números")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), ROUNDS).hex()
    return f"pdv2_pbkdf2_sha256${ROUNDS}${salt}${digest}"

def verify_pin(pin, encoded):
    try:
        if not isinstance(pin, str) or len(pin) > 12 or not isinstance(encoded, str) or len(encoded) > 200:
            return False
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pdv2_pbkdf2_sha256" or not 100000 <= int(rounds) <= 1000000 or len(salt) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False
