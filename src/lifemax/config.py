"""Application configuration loaded from a `.env` file at the project root."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
BACKUPS_DIR = DATA_DIR / "backups"
TASKS_FILE = DATA_DIR / "tasks.json"
HABITS_FILE = DATA_DIR / "habits.json"
QUOTES_FILE = DATA_DIR / "quotes.json"
STATIC_DIR = Path(__file__).resolve().parent / "server" / "static"

# Quote rotation: how many slots per local day. The dashboard rotates
# through `QUOTES_PER_DAY` deterministic picks; with cadence 4 the same
# date+slot combo always yields the same quote, so reloads are stable.
QUOTES_PER_DAY = 4

# Daily-checklist seed used on first launch when habits.json doesn't yet exist.
STARTER_HABITS: tuple[str, ...] = (
    "exercise",
    "meditate",
    "read 30 min",
    "drink 2L water",
    "no phone before bed",
    "journal",
)

# When does a "habit day" roll over? 3am local lets late-night check-offs still
# count for the previous calendar day.
HABIT_DAY_CUTOFF_HOUR = 3


# Curated AI news sources (RSS). Ordered roughly by image-richness +
# brand reliability — the slideshow leans on inline images, so feeds that
# embed real <media:content>/<img> hero shots come first. Hacker News is
# kept LAST as a long-tail safety net (it almost never has images and the
# news widget aggressively de-noises low-score / no-image HN entries).
DEFAULT_NEWS_FEEDS: tuple[str, ...] = (
    # Image-rich brand newsrooms — anchors the slideshow.
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://www.wired.com/feed/tag/ai/latest/rss",
    "https://arstechnica.com/ai/feed/",
    # Lab + research blogs — usually carry hero artwork.
    "https://www.anthropic.com/news/rss.xml",
    "https://openai.com/blog/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://huggingface.co/blog/feed.xml",
    # Indie + analysis (often no image — we'll OG-fallback when possible).
    "https://simonwillison.net/atom/everything/",
    "https://www.deeplearning.ai/the-batch/feed/",
    # Long-tail (rarely image-rich, kept for breadth — heavily filtered).
    "https://hnrss.org/newest?q=AI+OR+LLM+OR+Anthropic+OR+OpenAI+OR+agent&points=20",
)


class Settings(BaseSettings):
    """Runtime settings, loaded once at process start."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_user_id: int = Field(default=0, validation_alias="TELEGRAM_USER_ID")

    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="deepseek/deepseek-chat",
        validation_alias="OPENROUTER_MODEL",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        validation_alias="OPENROUTER_BASE_URL",
    )

    server_host: str = Field(default="127.0.0.1", validation_alias="SERVER_HOST")
    server_port: int = Field(default=8765, validation_alias="SERVER_PORT")
    dispatch_token: str = Field(default="", validation_alias="LIFEMAX_DISPATCH_TOKEN")

    latitude: float | None = Field(default=None, validation_alias="LIFEMAX_LATITUDE")
    longitude: float | None = Field(default=None, validation_alias="LIFEMAX_LONGITUDE")
    timezone: str = Field(default="America/New_York", validation_alias="LIFEMAX_TIMEZONE")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    news_feeds: tuple[str, ...] = DEFAULT_NEWS_FEEDS

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance, ensuring data dirs exist on first call."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
