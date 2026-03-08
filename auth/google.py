"""Google OAuth token verification using google-auth library."""

import logging

from fastapi import HTTPException
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

# Cache the transport session across calls
_transport = None


def _get_transport():
    global _transport
    if _transport is None:
        _transport = google_requests.Request()
    return _transport


def verify_google_token(credential: str, client_id: str) -> dict:
    """Verify a Google ID token and return user info.

    Returns: {"email": ..., "name": ..., "picture": ...}
    Raises HTTPException(401) on invalid token.
    """
    try:
        id_info = id_token.verify_oauth2_token(
            credential,
            _get_transport(),
            audience=client_id,
        )

        if id_info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise ValueError("Invalid issuer")

        return {
            "email": id_info["email"],
            "name": id_info.get("name"),
            "picture": id_info.get("picture"),
        }

    except ValueError as e:
        logger.warning("Google token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid Google credential") from e
