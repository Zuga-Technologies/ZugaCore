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
    """Create or update a user record. Returns the user."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        user = result.scalar_one_or_none()

        role = "admin" if _is_admin_email(email) else "user"

        if user is None:
            user = UserRecord(
                id=str(uuid.uuid4()),
                email=email,
                name=name,
                avatar_url=avatar_url,
                auth_provider=auth_provider,
                role=role,
            )
            session.add(user)
        else:
            if name is not None:
                user.name = name
            if avatar_url is not None:
                user.avatar_url = avatar_url
            user.auth_provider = auth_provider
            user.role = role

        return user


async def get_user_by_email(email: str) -> UserRecord | None:
    """Look up a user by email."""
    async with get_session() as session:
        result = await session.execute(
            select(UserRecord).where(UserRecord.email == email)
        )
        return result.scalar_one_or_none()
