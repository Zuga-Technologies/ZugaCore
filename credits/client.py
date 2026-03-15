"""Dual-mode credit client for studios.

Provides a unified interface for credit tracking that works in both
embedded mode (shared DB with ZugaApp) and standalone mode (HTTP calls).

Usage:
    from core.credits.client import get_credit_client

    client = get_credit_client()
    if not await client.can_spend(user_id, email):
        raise CreditBlockedError("No credits")

    response = await ai_call(...)

    await client.record_spend(
        user_id=user_id,
        credits=dollars_to_credits(response.cost),
        cost_usd=response.cost,
        service="venice",
        model=response.model,
        reason="therapist",
    )
"""

import abc
import logging
import os

logger = logging.getLogger(__name__)

DOLLARS_TO_CREDITS = 1000


def dollars_to_credits(usd: float) -> float:
    return usd * DOLLARS_TO_CREDITS


class CreditClient(abc.ABC):
    """Abstract credit client — same interface for all modes."""

    @abc.abstractmethod
    async def can_spend(self, user_id: str, email: str, estimated_credits: float = 0) -> bool:
        ...

    @abc.abstractmethod
    async def record_spend(
        self,
        user_id: str,
        credits: float,
        cost_usd: float,
        service: str,
        reason: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        ...


class DirectCreditClient(CreditClient):
    """Shared DB mode — writes directly to the credit ledger.

    Used when running embedded inside ZugaApp (shared SQLite DB).
    """

    async def can_spend(self, user_id: str, email: str, estimated_credits: float = 0) -> bool:
        from core.credits.manager import can_spend
        return await can_spend(user_id, email, estimated_credits)

    async def record_spend(
        self,
        user_id: str,
        credits: float,
        cost_usd: float,
        service: str,
        reason: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        from core.credits.manager import record_spend
        await record_spend(
            user_id=user_id,
            credits=credits,
            cost_usd=cost_usd,
            service=service,
            reason=reason,
            model=model,
            metadata=metadata,
        )


class HttpCreditClient(CreditClient):
    """HTTP mode — calls ZugaApp's credit API endpoints.

    Used when running standalone (own process, own DB).
    """

    def __init__(self, base_url: str, service_key: str = ""):
        self._base_url = base_url.rstrip("/")
        self._service_key = service_key

    async def can_spend(self, user_id: str, email: str, estimated_credits: float = 0) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/credits/can-spend",
                    json={"user_id": user_id, "email": email, "estimated_credits": estimated_credits},
                    headers=self._headers,
                )
                resp.raise_for_status()
                return resp.json().get("allowed", False)
        except Exception as e:
            logger.warning("HTTP can_spend failed: %s", e)
            return self._fail_open

    async def record_spend(
        self,
        user_id: str,
        credits: float,
        cost_usd: float,
        service: str,
        reason: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self._base_url}/api/credits/report-spend",
                    json={
                        "user_id": user_id,
                        "credits": credits,
                        "cost_usd": cost_usd,
                        "service": service,
                        "reason": reason,
                        "model": model,
                        "metadata": metadata,
                    },
                    headers=self._headers,
                )
        except Exception as e:
            logger.warning("HTTP record_spend failed (spend NOT tracked): %s", e)

    @property
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._service_key:
            h["X-Service-Key"] = self._service_key
        return h

    @property
    def _fail_open(self) -> bool:
        return os.environ.get("CREDIT_FAIL_MODE", "open") != "closed"


class NullCreditClient(CreditClient):
    """No-op client — logs spend but doesn't gate or persist.

    Used when neither DB nor HTTP is available (pure dev mode).
    """

    async def can_spend(self, user_id: str, email: str, estimated_credits: float = 0) -> bool:
        return True

    async def record_spend(
        self,
        user_id: str,
        credits: float,
        cost_usd: float,
        service: str,
        reason: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        logger.info(
            "[NullCredits] user=%s credits=%.1f ($%.4f) service=%s reason=%s",
            user_id, credits, cost_usd, service, reason,
        )


# ── Singleton factory ───────────────────────────────────────────────────

_instance: CreditClient | None = None


def get_credit_client() -> CreditClient:
    """Auto-detect mode and return the appropriate credit client.

    Detection order:
    1. ZUGAAPP_CREDITS_URL set → HTTP mode
    2. DB engine initialized → Direct mode (embedded in ZugaApp)
    3. Neither → Null mode (logging only)
    """
    global _instance
    if _instance is not None:
        return _instance

    # Check for HTTP mode first (explicit config wins)
    credits_url = os.environ.get("ZUGAAPP_CREDITS_URL", "").strip()
    service_key = os.environ.get("STUDIO_SERVICE_KEY", "").strip()

    if credits_url:
        logger.info("Credit client: HTTP mode → %s", credits_url)
        _instance = HttpCreditClient(credits_url, service_key)
        return _instance

    # Try direct DB mode
    try:
        from core.database.session import _engine
        if _engine is not None:
            logger.info("Credit client: Direct DB mode")
            _instance = DirectCreditClient()
            return _instance
    except (ImportError, AttributeError):
        pass

    # Fallback to null
    logger.info("Credit client: Null mode (logging only)")
    _instance = NullCreditClient()
    return _instance


def reset_credit_client() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None
