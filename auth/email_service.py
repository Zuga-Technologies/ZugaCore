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


_BRAND_LOGO_URL = "https://zugatechnologies.com/zuga-technologies-logo-400.png"
_BRAND_ACCENT = "#06b6d4"        # bible §2.4 / §4.2 cyan-500 — master Zuga accent
_BRAND_ACCENT_DIM = "#0e7490"    # bible §4.2 cyan-700 — fallback link / hover
_FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _render_header() -> str:
    """Bible §2.2/§2.3 — master Z mark + ZUGA wordmark in cyan, on master dark surface."""
    return f"""\
          <tr>
            <td align="center" style="padding-bottom:32px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
                <tr>
                  <td valign="middle" style="padding-right:12px;">
                    <img src="{_BRAND_LOGO_URL}" width="40" height="40" alt="Zuga"
                         style="display:block;border-radius:9px;border:0;">
                  </td>
                  <td valign="middle" style="font-family:{_FONT_STACK};font-size:18px;font-weight:700;letter-spacing:0.05em;color:{_BRAND_ACCENT};">
                    ZUGA
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""


def _render_footer() -> str:
    """Bible-aligned footer — corp landing pattern (copyright + nav)."""
    return f"""\
          <tr>
            <td align="center" style="padding-top:28px;font-family:{_FONT_STACK};">
              <p style="margin:0 0 8px;font-size:12px;line-height:1.5;color:#525252;">
                &copy; 2026 Zuga Technologies LLC
              </p>
              <p style="margin:0;font-size:12px;line-height:1.5;color:#525252;">
                <a href="https://zugatechnologies.com" style="color:#737373;text-decoration:none;">zugatechnologies.com</a>
                &nbsp;·&nbsp;
                <a href="https://zugabot.ai" style="color:#737373;text-decoration:none;">zugabot.ai</a>
                &nbsp;·&nbsp;
                <a href="mailto:buga@zugabot.ai" style="color:#737373;text-decoration:none;">buga@zugabot.ai</a>
              </p>
            </td>
          </tr>"""


def _email_template(
    title: str,
    body: str,
    button_text: str,
    button_link: str,
    footer: str,
    secondary_button_text: str | None = None,
    secondary_button_link: str | None = None,
) -> str:
    """Render a branded Zuga email — bible-aligned (cyan master accent, dark surface).

    Optional secondary button renders below the primary as a ghost-style outline
    button (used by the admin Approve/Deny notification).
    """
    safe_link = html_lib.escape(button_link)

    secondary_block = ""
    if secondary_button_text and secondary_button_link:
        safe_sec_link = html_lib.escape(secondary_button_link)
        secondary_block = f"""
                <tr>
                  <td align="center" style="padding-top:10px;">
                    <a href="{safe_sec_link}"
                       style="display:inline-block;padding:12px 36px;background-color:transparent;color:#a3a3a3;font-size:14px;font-weight:500;text-decoration:none;border:1px solid #333;border-radius:8px;">
                      {html_lib.escape(secondary_button_text)}
                    </a>
                  </td>
                </tr>"""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html_lib.escape(title)}</title>
</head>
<body style="margin:0;padding:0;background-color:#0a0a0a;font-family:{_FONT_STACK};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#0a0a0a;">
    <tr>
      <td align="center" style="padding:40px 20px;">
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;">
{_render_header()}
          <tr>
            <td style="background-color:#111111;border:1px solid #262626;border-radius:16px;padding:40px 36px;">

              <h1 style="margin:0 0 8px;font-size:22px;font-weight:600;color:#f5f5f5;text-align:center;">
                {html_lib.escape(title)}
              </h1>

              <p style="margin:0 0 28px;font-size:15px;line-height:1.6;color:#a3a3a3;text-align:center;">
                {body}
              </p>

              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="{safe_link}"
                       style="display:inline-block;padding:12px 32px;background-color:{_BRAND_ACCENT};color:#0a0a0a;font-size:15px;font-weight:600;text-decoration:none;border-radius:8px;">
                      {html_lib.escape(button_text)}
                    </a>
                  </td>
                </tr>{secondary_block}
              </table>

              <p style="margin:24px 0 0;font-size:13px;color:#525252;text-align:center;">
                {html_lib.escape(footer)}
              </p>

              <p style="margin:16px 0 0;font-size:12px;color:#525252;text-align:center;word-break:break-all;">
                If the button doesn't work, copy this link:<br>
                <a href="{safe_link}" style="color:{_BRAND_ACCENT_DIM};text-decoration:underline;">{safe_link}</a>
              </p>

            </td>
          </tr>
{_render_footer()}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


async def send_verification_email(email: str, token: str) -> None:
    """Send an email verification link."""
    base = _get_base_url()
    link = f"{base}/verify-email?token={token}"

    html = _email_template(
        title="Verify your email",
        body="Welcome to ZugaApp. Tap the button below to verify your email address and activate your account.",
        button_text="Verify my email",
        button_link=link,
        footer="This link expires in 24 hours. If you didn't create an account, you can ignore this email.",
    )

    await _send(email, "Verify your ZugaApp account", html)


async def send_reset_email(email: str, token: str) -> None:
    """Send a password reset link."""
    base = _get_base_url()
    link = f"{base}/reset-password?token={token}"

    html = _email_template(
        title="Reset your password",
        body="We received a request to reset your ZugaApp password. Tap the button below to choose a new one.",
        button_text="Reset my password",
        button_link=link,
        footer="This link expires in 1 hour. If you didn't request this, you can safely ignore this email.",
    )

    await _send(email, "Reset your ZugaApp password", html)


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
        logger.info("Email sent to %s: %s", to, subject)
    except Exception as exc:
        logger.error("Resend failed for %s: %s", to, exc)
        raise
