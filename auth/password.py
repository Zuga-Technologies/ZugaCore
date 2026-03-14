"""Password hashing and validation via passlib + bcrypt."""

from passlib.hash import bcrypt

MIN_PASSWORD_LENGTH = 8

# Pre-computed hash used to equalize timing on failed lookups.
# Prevents attackers from distinguishing "email not found" (fast)
# from "wrong password" (slow bcrypt) via response time.
DUMMY_HASH = bcrypt.hash("__timing_equalizer__")


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return bcrypt.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    return bcrypt.verify(plain, hashed)


def validate_password(plain: str) -> str | None:
    """Return an error message if the password is invalid, else None."""
    if len(plain) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None
