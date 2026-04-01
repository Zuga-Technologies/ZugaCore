"""AI Gateway client — drop-in replacement for direct AI calls in studios.

Studios import this instead of calling Venice/OpenAI directly. The client
auto-detects whether to call the gateway (hosted mode) or providers
directly (embedded/dev mode).

Usage in studios:
    from core.gateway.client import gateway_ai_call, gateway_tts_call

    # Same interface as the old ai_call() — drop-in replacement
    result = await gateway_ai_call(
        prompt="Analyze this mood entry...",
        task="journal_reflection",
        user_id=user.id,
        user_email=user.email,
    )
    print(result.content)
"""

import base64
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    tokens_charged: float = 0


@dataclass
class TTSResponse:
    audio_bytes: bytes
    model: str
    characters: int
    cost_usd: float
    tokens_charged: float = 0


def _get_gateway_url() -> str | None:
    """Get the AI Gateway URL if configured. None means use direct mode."""
    return os.environ.get("ZUGAAPP_GATEWAY_URL", "").strip() or None


def _get_auth_token() -> str:
    """Get the auth token for gateway calls."""
    return os.environ.get("STUDIO_SERVICE_KEY", "").strip()


async def gateway_ai_call(
    prompt: str = "",
    task: str = "chat",
    max_tokens: int = 4096,
    messages: list[dict] | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
    model: str | None = None,
    auth_token: str | None = None,
) -> AIResponse:
    """Make an AI call — routes through gateway or calls Venice directly.

    Drop-in replacement for the old ai_call() function.
    """
    gateway_url = _get_gateway_url()

    if gateway_url:
        return await _gateway_chat(
            gateway_url, prompt, task, max_tokens, messages, model,
            auth_token or _get_auth_token(),
        )
    else:
        return await _direct_chat(
            prompt, task, max_tokens, messages, model, user_id, user_email,
        )


async def gateway_tts_call(
    text: str,
    voice: str = "shimmer",
    model: str = "tts-1-hd",
    speed: float = 1.0,
    instruction: str = "",
    user_id: str | None = None,
    user_email: str | None = None,
    auth_token: str | None = None,
) -> TTSResponse:
    """Make a TTS call — routes through gateway or calls OpenAI directly."""
    gateway_url = _get_gateway_url()

    if gateway_url:
        return await _gateway_tts(
            gateway_url, text, voice, model, speed, instruction,
            auth_token or _get_auth_token(),
        )
    else:
        return await _direct_tts(text, voice, model, speed, instruction, user_id, user_email)


# ── Gateway Mode (HTTP calls to zugabot.ai) ─────────────────────────


async def _gateway_chat(
    gateway_url: str,
    prompt: str,
    task: str,
    max_tokens: int,
    messages: list[dict] | None,
    model: str | None,
    auth_token: str,
) -> AIResponse:
    """Call the AI gateway's /api/ai/chat endpoint."""
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{gateway_url}/api/ai/chat",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "prompt": prompt,
                "messages": messages,
                "task": task,
                "max_tokens": max_tokens,
                "model": model,
            },
        )

        if response.status_code == 402:
            data = response.json().get("detail", {})
            raise InsufficientTokensError(
                message=data.get("message", "Insufficient tokens"),
                balance=data.get("balance", {}),
                estimated_cost=data.get("estimated_cost", 0),
            )

        response.raise_for_status()
        data = response.json()

    return AIResponse(
        content=data["content"],
        model=data["model"],
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        cost_usd=0,  # gateway handles billing
        tokens_charged=data.get("tokens_charged", 0),
    )


async def _gateway_tts(
    gateway_url: str,
    text: str,
    voice: str,
    model: str,
    speed: float,
    instruction: str,
    auth_token: str,
) -> TTSResponse:
    """Call the AI gateway's /api/ai/tts endpoint."""
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{gateway_url}/api/ai/tts",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={
                "text": text,
                "voice": voice,
                "model": model,
                "speed": speed,
                "instruction": instruction,
            },
        )

        if response.status_code == 402:
            data = response.json().get("detail", {})
            raise InsufficientTokensError(
                message=data.get("message", "Insufficient tokens"),
                balance=data.get("balance", {}),
                estimated_cost=data.get("estimated_cost", 0),
            )

        response.raise_for_status()
        data = response.json()

    audio_bytes = base64.b64decode(data["audio_base64"])

    return TTSResponse(
        audio_bytes=audio_bytes,
        model=data["model"],
        characters=data["characters"],
        cost_usd=0,
        tokens_charged=data.get("tokens_charged", 0),
    )


# ── Direct Mode (local provider calls, for embedded/dev) ────────────


async def _direct_chat(
    prompt: str,
    task: str,
    max_tokens: int,
    messages: list[dict] | None,
    model: str | None,
    user_id: str | None,
    user_email: str | None,
) -> AIResponse:
    """Call Venice directly (embedded in ZugaApp or dev mode)."""
    from core.gateway.providers import call_venice

    if messages:
        msgs = messages
    elif prompt:
        msgs = [{"role": "user", "content": prompt}]
    else:
        raise ValueError("Either prompt or messages required")

    result = await call_venice(messages=msgs, model=model, max_tokens=max_tokens)

    return AIResponse(
        content=result.content,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )


async def _direct_tts(
    text: str,
    voice: str,
    model: str,
    speed: float,
    instruction: str,
    user_id: str | None,
    user_email: str | None,
) -> TTSResponse:
    """Call OpenAI TTS directly (embedded in ZugaApp or dev mode)."""
    from core.gateway.providers import call_openai_tts

    result = await call_openai_tts(
        text=text, voice=voice, model=model, speed=speed, instruction=instruction,
    )

    return TTSResponse(
        audio_bytes=result.audio_bytes,
        model=result.model,
        characters=result.characters,
        cost_usd=result.cost_usd,
    )


# ── Exceptions ───────────────────────────────────────────────────────


class InsufficientTokensError(Exception):
    """Raised when a user doesn't have enough tokens for an AI call."""

    def __init__(self, message: str, balance: dict, estimated_cost: float):
        super().__init__(message)
        self.balance = balance
        self.estimated_cost = estimated_cost
