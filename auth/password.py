"""Password hashing and validation via bcrypt directly.

passlib is abandonware (last updated 2020) and broken with bcrypt >= 4.1.
We use the bcrypt library's own hashpw/checkpw instead.
"""

import bcrypt

MIN_PASSWORD_LENGTH = 8

# Pre-computed hash used to equalize timing on failed lookups.
# Prevents attackers from distinguishing "email not found" (fast)
# from "wrong password" (slow bcrypt) via response time.
DUMMY_HASH = bcrypt.hashpw(b"__timing_equalizer__", bcrypt.gensalt()).decode()


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def validate_password(plain: str) -> str | None:
    """Return an error message if the password is invalid, else None."""
    if len(plain) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None
