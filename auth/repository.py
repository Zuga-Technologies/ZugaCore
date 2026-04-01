"""User persistence — upsert and lookup by email."""

import os
import uuid

from sqlalchemy import select

from core.auth.models import UserRecord
from core.database.session import get_session


def _is_admin_email(email: str) -> bool:
    """Check if email is in the ADMIN_EMAILS env var (comma-separated)."""
    admin_emails = os.environ.get("ADMIN_EMAILS", "")
    if not admin_emails:
        return False
    return email.lower() in [e.strip().lower() for e in admin_emails.split(",")]


async def upsert_user(
    email: str,
    name: str | None = None,
    avatar_url: str | None = None,
    auth_provider: str = "dev",
) -> UserRecord:
    """Create or update a user record. Returns the user.

    Google users are auto-verified (trusted identity provider).
    """
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        user = result.scalar_one_or_none()

        role = "admin" if _is_admin_email(email) else "user"
        auto_verify = auth_provider == "google"

        if user is None:
            user = UserRecord(
                id=str(uuid.uuid4()),
                email=email,
                name=name,
                avatar_url=avatar_url,
                auth_provider=auth_provider,
                role=role,
                email_verified=auto_verify,
            )
            session.add(user)
        else:
            if name is not None:
                user.name = name
            if avatar_url is not None:
                user.avatar_url = avatar_url
            user.auth_provider = auth_provider
            user.role = role
            if auto_verify:
                user.email_verified = True

        return user


async def get_user_by_email(email: str) -> UserRecord | None:
    """Look up a user by email."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        return result.scalar_one_or_none()


async def create_user_with_password(
    email: str,
    password_hash: str,
) -> UserRecord:
    """Create a new password-based user. Raises ValueError if email exists."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        if result.scalar_one_or_none() is not None:
            raise ValueError("An account with this email already exists")

        role = "admin" if _is_admin_email(email) else "user"
        user = UserRecord(
            id=str(uuid.uuid4()),
            email=email,
            auth_provider="password",
            role=role,
            password_hash=password_hash,
            email_verified=False,
        )
        session.add(user)
        return user


async def set_password_hash(email: str, password_hash: str) -> None:
    """Update a user's password hash."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        user.password_hash = password_hash
        user.auth_provider = "password"


async def set_email_verified(email: str) -> None:
    """Mark a user's email as verified."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        user.email_verified = True


async def get_user_by_supertokens_id(st_user_id: str) -> UserRecord | None:
    """Look up a user by their SuperTokens user ID."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.supertokens_user_id == st_user_id)
        )
        return result.scalar_one_or_none()


async def get_user_by_id(user_id: str) -> UserRecord | None:
    """Look up a user by their app-level ID."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.id == user_id)
        )
        return result.scalar_one_or_none()


async def link_supertokens_id(email: str, st_user_id: str) -> None:
    """Link a SuperTokens user ID to an existing user record."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            user.supertokens_user_id = st_user_id


async def get_onboarding_state(user_id: str) -> bool:
    """Return whether the user has completed app-level onboarding."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord.onboarding_completed).where(UserRecord.id == user_id)
        )
        value = result.scalar_one_or_none()
        return bool(value)


async def set_onboarding_state(user_id: str, completed: bool) -> None:
    """Mark app-level onboarding as completed or reset it."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.id == user_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError("User not found")
        user.onboarding_completed = completed
