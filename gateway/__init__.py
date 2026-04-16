"""AI Gateway — token-gated proxy to Venice (chat) and OpenAI (TTS).

Studios don't import from here — the gateway is consumed via HTTP by
browser-side callers (ZugaExtension, ZugaLife onboarding). The router is
mounted into ZugaApp's FastAPI app at /api/ai/*.
"""

from core.gateway.providers import (
    AIResponse,
    TTSResponse,
    call_openai_tts,
    call_venice,
    estimate_chat_cost,
    estimate_chat_tokens_from_prompt,
    estimate_tts_cost,
)
from core.gateway.routes import router

__all__ = [
    "router",
    "AIResponse",
    "TTSResponse",
    "call_venice",
    "call_openai_tts",
    "estimate_chat_cost",
    "estimate_chat_tokens_from_prompt",
    "estimate_tts_cost",
]
