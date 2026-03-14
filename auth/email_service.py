"""Email sending via Resend SDK.

Falls back to console logging if RESEND_API_KEY is not set (dev mode).
"""

import html as html_lib
import logging
import os

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://localhost:5173")


def _get_resend_api_key() -> str | None:
    return os.environ.get("RESEND_API_KEY") or None


def _get_from_address() -> str:
    return os.environ.get("EMAIL_FROM", "ZugaApp <noreply@zugabot.ai>")


async def send_verification_email(email: str, token: str) -> None:
    """Send an email verification link."""
    base = _get_base_url()
    safe_link = html_lib.escape(f"{base}/verify-email?token={token}")

    subject = "Verify your ZugaApp account"
    html = (
        "<h2>Welcome to ZugaApp</h2>"
        "<p>Click the link below to verify your email address:</p>"
        f'<p><a href="{safe_link}">Verify my email</a></p>'
        "<p>This link expires in 24 hours.</p>"
        "<p>If you didn't create an account, you can ignore this email.</p>"
    )

    await _send(email, subject, html)


async def send_reset_email(email: str, token: str) -> None:
    """Send a password reset link."""
    base = _get_base_url()
    safe_link = html_lib.escape(f"{base}/reset-password?token={token}")

    subject = "Reset your ZugaApp password"
    html = (
        "<h2>Password Reset</h2>"
        "<p>Click the link below to reset your password:</p>"
        f'<p><a href="{safe_link}">Reset my password</a></p>'
        "<p>This link expires in 1 hour.</p>"
        "<p>If you didn't request this, you can ignore this email.</p>"
    )

    await _send(email, subject, html)


async def _send(to: str, subject: str, html: str) -> None:
    """Send an email via Resend, or log to console in dev mode."""
    api_key = _get_resend_api_key()

    if not api_key:
        logger.warning("[EMAIL DEV MODE] To: %s | Subject: %s", to, subject)
        logger.warning("[EMAIL DEV MODE] HTML: %s", html)
        return

    import resend
    resend.api_key = api_key

    params = {
        "from": _get_from_address(),
        "to": [to],
        "subject": subject,
        "html": html,
    }

    try:
        resend.Emails.send(params)
        print(f"[EMAIL] Sent to {to}: {subject}")
    except Exception as exc:
        # Resend fails if domain not verified — fall back to console
        print(f"[EMAIL FALLBACK] Resend failed ({exc}), logging instead:")
        print(f"[EMAIL FALLBACK] To: {to} | Subject: {subject}")
        print(f"[EMAIL FALLBACK] HTML: {html}")
