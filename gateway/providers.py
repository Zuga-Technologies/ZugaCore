"""AI provider adapters — Venice (chat) and OpenAI (TTS).

These make the actual HTTP calls to AI providers. The gateway routes
call these after token gating has been verified.
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# ── Response Types ───────────────────────────────────────────────────


@dataclass
class AIResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class TTSResponse:
    audio_bytes: bytes
    model: str
    characters: int
    cost_usd: float


# ── Cost Estimation ──────────────────────────────────────────────────

# Venice AI pricing (per 1M tokens)
VENICE_COST_PER_1M_INPUT = 0.27
VENICE_COST_PER_1M_OUTPUT = 1.10

# OpenAI TTS pricing (per 1M characters)
TTS_COST_PER_1M_CHARS = {"tts-1": 15.0, "tts-1-hd": 30.0, "gpt-4o-mini-tts": 12.0}


def estimate_chat_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * VENICE_COST_PER_1M_INPUT / 1_000_000) + (
        output_tokens * VENICE_COST_PER_1M_OUTPUT / 1_000_000
    )


def estimate_tts_cost(characters: int, model: str = "tts-1-hd") -> float:
    rate = TTS_COST_PER_1M_CHARS.get(model, 30.0)
    return characters * rate / 1_000_000


def estimate_chat_tokens_from_prompt(prompt: str, messages: list[dict] | None) -> int:
    """Rough pre-call estimate for token gating. ~4 chars per token."""
    if messages:
        total_chars = sum(len(m.get("content", "")) for m in messages)
    else:
        total_chars = len(prompt)
    return max(total_chars // 4, 10)


# ── Venice AI (Chat Completions) ─────────────────────────────────────

VENICE_API_URL = "https://api.venice.ai/api/v1/chat/completions"
VENICE_DEFAULT_MODEL = "kimi-k2-5"


async def call_venice(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 4096,
) -> AIResponse:
    """Call Venice AI chat completion API."""
    api_key = os.environ.get("VENICE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("VENICE_API_KEY not set")

    model = model or VENICE_DEFAULT_MODEL

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            VENICE_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            },
        )
        response.raise_for_status()
        data = response.json()

    choice = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    cost = estimate_chat_cost(input_tokens, output_tokens)

    return AIResponse(
        content=choice,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


# ── OpenAI TTS ───────────────────────────────────────────────────────

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
TTS_MAX_CHUNK = 4000  # OpenAI's per-request character limit


def _split_tts_chunks(text: str, max_chars: int = TTS_MAX_CHUNK) -> list[str]:
    """Split text at paragraph boundaries for TTS chunking."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 > max_chars:
            if current:
                chunks.append(current.strip())
            current = paragraph
        else:
            current = current + "\n\n" + paragraph if current else paragraph

    if current.strip():
        chunks.append(current.strip())

    return chunks or [text[:max_chars]]


async def call_openai_tts(
    text: str,
    voice: str = "shimmer",
    model: str = "tts-1-hd",
    speed: float = 1.0,
    instruction: str = "",
) -> TTSResponse:
    """Call OpenAI TTS API. Automatically chunks long text."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    chunks = _split_tts_chunks(text)
    all_audio = bytearray()
    total_chars = 0

    async with httpx.AsyncClient(timeout=120) as client:
        for chunk in chunks:
            body: dict = {
                "model": model,
                "input": chunk,
                "voice": voice,
                "response_format": "mp3",
                "speed": speed,
            }
            if instruction and "gpt-4o" in model:
                body["instructions"] = instruction

            response = await client.post(
                OPENAI_TTS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            all_audio.extend(response.content)
            total_chars += len(chunk)

    cost = estimate_tts_cost(total_chars, model)

    return TTSResponse(
        audio_bytes=bytes(all_audio),
        model=model,
        characters=total_chars,
        cost_usd=cost,
    )
