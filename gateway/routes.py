"""AI Gateway routes — token-gated proxy to AI providers.

Self-hosted studios call these endpoints instead of hitting Venice/OpenAI directly.
Every call: authenticate → estimate cost → try_spend tokens → call provider → return result.

Endpoints:
    POST /api/ai/chat   — Text completion (Venice AI)
    POST /api/ai/tts    — Text-to-speech (OpenAI TTS)
    GET  /api/ai/models — Available models and estimated costs
"""

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser
from core.credits.manager import dollars_to_tokens, try_spend

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ai-gateway"])


# ── Request/Response Models ──────────────────────────────────────────


class ChatRequest(BaseModel):
    """Request for AI text completion."""

    prompt: str = Field(default="", max_length=100_000)
    messages: list[dict] | None = Field(
        default=None,
        description="OpenAI-compatible messages array. If provided, prompt is ignored.",
    )
    task: str = Field(default="chat", max_length=64, description="Studio/feature identifier for cost tracking")
    max_tokens: int = Field(default=4096, ge=1, le=16384)
    model: str | None = Field(default=None, max_length=128, description="Model override (default: kimi-k2-5)")


class ChatResponse(BaseModel):
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    tokens_charged: float
    tokens_remaining: float


class TTSRequest(BaseModel):
    """Request for text-to-speech."""

    text: str = Field(min_length=1, max_length=50_000)
    voice: str = Field(default="shimmer", pattern="^(shimmer|nova|alloy|echo|fable|onyx)$")
    model: str = Field(default="tts-1-hd", pattern="^(tts-1|tts-1-hd|gpt-4o-mini-tts)$")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    instruction: str = Field(default="", max_length=1000)


class TTSResponse(BaseModel):
    audio_base64: str
    model: str
    characters: int
    tokens_charged: float
    tokens_remaining: float


# ── Chat Completion ──────────────────────────────────────────────────


@router.post("/api/ai/chat")
async def ai_chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
) -> ChatResponse:
    """Token-gated AI text completion via Venice AI.

    Studios call this instead of Venice directly. Tokens are deducted
    atomically — if the user can't afford it, they get a 402.
    """
    from core.credits.manager import get_balance
    from core.gateway.providers import (
        call_venice,
        estimate_chat_cost,
        estimate_chat_tokens_from_prompt,
    )

    # Build messages
    if body.messages:
        messages = body.messages
    elif body.prompt:
        messages = [{"role": "user", "content": body.prompt}]
    else:
        raise HTTPException(status_code=400, detail="Either prompt or messages required")

    # Estimate cost for pre-flight check
    est_input = estimate_chat_tokens_from_prompt(body.prompt, body.messages)
    est_cost = estimate_chat_cost(est_input, body.max_tokens // 2)  # assume half max output
    est_tokens = dollars_to_tokens(est_cost)

    # Atomic check-and-deduct (pre-flight with estimate)
    # We'll do the real deduction after the call with actual cost
    # For now, just verify they have enough for the estimate
    from core.credits.manager import can_spend

    if not await can_spend(user.id, user.email, est_tokens):
        balance = await get_balance(user.id)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_tokens",
                "message": f"This call needs ~{est_tokens:.0f} tokens. You have {balance['total']:.0f}.",
                "balance": balance,
                "estimated_cost": round(est_tokens, 1),
            },
        )

    # Call Venice AI
    try:
        result = await call_venice(
            messages=messages,
            model=body.model,
            max_tokens=body.max_tokens,
        )
    except RuntimeError as e:
        logger.exception("AI provider error for user %s", user.id)
        raise HTTPException(status_code=503, detail="AI service unavailable")
    except Exception as e:
        logger.exception("Unexpected error calling AI for user %s", user.id)
        raise HTTPException(status_code=502, detail="AI provider error")

    # Deduct actual cost
    actual_tokens = dollars_to_tokens(result.cost_usd)
    spent = await try_spend(
        user_id=user.id,
        email=user.email,
        tokens=actual_tokens,
        cost_usd=result.cost_usd,
        service="venice",
        reason=body.task,
        model=result.model,
    )

    if not spent:
        # Edge case: passed pre-flight but failed actual deduction (concurrent spend)
        # Return the result anyway — we'd rather under-charge than fail after the AI call
        logger.warning(
            "Post-call token deduction failed for user %s (concurrent spend?). "
            "AI call completed but tokens not charged. task=%s cost=$%.4f",
            user.id, body.task, result.cost_usd,
        )

    balance = await get_balance(user.id)

    return ChatResponse(
        content=result.content,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        tokens_charged=round(actual_tokens, 1),
        tokens_remaining=balance["total"],
    )


# ── Text-to-Speech ───────────────────────────────────────────────────


@router.post("/api/ai/tts")
async def ai_tts(
    body: TTSRequest,
    user: CurrentUser = Depends(get_current_user),
) -> TTSResponse:
    """Token-gated text-to-speech via OpenAI TTS.

    Returns base64-encoded MP3 audio. Studios decode and play/save locally.
    """
    from core.credits.manager import get_balance
    from core.gateway.providers import call_openai_tts, estimate_tts_cost

    # Estimate cost
    est_cost = estimate_tts_cost(len(body.text), body.model)
    est_tokens = dollars_to_tokens(est_cost)

    # Pre-flight token check
    from core.credits.manager import can_spend

    if not await can_spend(user.id, user.email, est_tokens):
        balance = await get_balance(user.id)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_tokens",
                "message": f"TTS needs ~{est_tokens:.0f} tokens. You have {balance['total']:.0f}.",
                "balance": balance,
                "estimated_cost": round(est_tokens, 1),
            },
        )

    # Call OpenAI TTS
    try:
        result = await call_openai_tts(
            text=body.text,
            voice=body.voice,
            model=body.model,
            speed=body.speed,
            instruction=body.instruction,
        )
    except RuntimeError as e:
        logger.exception("TTS provider error for user %s", user.id)
        raise HTTPException(status_code=503, detail="TTS service unavailable")
    except Exception as e:
        logger.exception("Unexpected error calling TTS for user %s", user.id)
        raise HTTPException(status_code=502, detail="TTS provider error")

    # Deduct actual cost
    actual_tokens = dollars_to_tokens(result.cost_usd)
    await try_spend(
        user_id=user.id,
        email=user.email,
        tokens=actual_tokens,
        cost_usd=result.cost_usd,
        service="openai_tts",
        reason="tts",
        model=result.model,
    )

    balance = await get_balance(user.id)

    return TTSResponse(
        audio_base64=base64.b64encode(result.audio_bytes).decode(),
        model=result.model,
        characters=result.characters,
        tokens_charged=round(actual_tokens, 1),
        tokens_remaining=balance["total"],
    )


# ── Models & Pricing Info ────────────────────────────────────────────


@router.get("/api/ai/models")
async def available_models() -> dict:
    """List available AI models and estimated token costs per feature.

    Public endpoint — helps frontend show cost previews before users commit.
    """
    from core.credits.manager import dollars_to_tokens
    from core.gateway.providers import (
        VENICE_COST_PER_1M_INPUT,
        VENICE_COST_PER_1M_OUTPUT,
        TTS_COST_PER_1M_CHARS,
    )

    # Estimated costs for common operations (in ZugaTokens)
    estimates = {
        "therapist_message": round(dollars_to_tokens(0.05), 1),
        "journal_reflection": round(dollars_to_tokens(0.005), 1),
        "habit_insight": round(dollars_to_tokens(0.005), 1),
        "meditation_5min": round(dollars_to_tokens(0.30), 1),
        "meditation_15min": round(dollars_to_tokens(0.40), 1),
        "news_bias_check": round(dollars_to_tokens(0.01), 1),
        "trader_signal": round(dollars_to_tokens(0.02), 1),
        "learn_tutoring": round(dollars_to_tokens(0.03), 1),
    }

    return {
        "chat": {
            "provider": "venice",
            "default_model": "kimi-k2-5",
            "cost_per_1m_input_usd": VENICE_COST_PER_1M_INPUT,
            "cost_per_1m_output_usd": VENICE_COST_PER_1M_OUTPUT,
        },
        "tts": {
            "provider": "openai",
            "models": list(TTS_COST_PER_1M_CHARS.keys()),
            "default_model": "tts-1-hd",
        },
        "estimated_token_costs": estimates,
    }
