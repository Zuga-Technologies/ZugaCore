"""Pydantic schemas for the theme override API."""

from pydantic import BaseModel, Field


VALID_SCOPES = {
    "app", "life", "trader", "news", "learn", "gamer",
    "code", "operator", "health", "video", "image", "overseer",
    "audio", "motion", "data", "slides", "docs",
}


class ThemeOverrideResponse(BaseModel):
    id: int
    scope: str
    css_override: str
    theme_name: str
    font: str | None = None
    preset_id: str | None = None

    model_config = {"from_attributes": True}


class ThemeOverrideUpsert(BaseModel):
    css_override: str = Field(..., max_length=10_000)
    theme_name: str = Field(default="Custom", max_length=100)
    font: str | None = Field(default=None, max_length=50)
    preset_id: str | None = Field(default=None, max_length=30)


class InternalApplyThemeRequest(BaseModel):
    """Service-to-service request from Zugabot."""
    user_id: str = Field(..., max_length=64)
    scope: str = Field(default="app", max_length=30)
    css_override: str = Field(..., max_length=10_000)
    theme_name: str = Field(default="Custom", max_length=100)
    font: str | None = Field(default=None, max_length=50)
    preset_id: str | None = Field(default=None, max_length=30)
