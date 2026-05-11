import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_env_local() -> None:
    """Load .env.local from the project root when running locally."""
    try:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parent.parent / ".env.local"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except ImportError:
        pass


_load_env_local()


_VALID_LANGUAGES = {"ko", "en"}
_VALID_PROVIDERS = {"openai", "bedrock", "xai"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid int for %s=%r, using default=%d", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%d below minimum %d, using minimum", name, value, minimum)
        return minimum
    return value


def _tz_env(name: str, default: str) -> str:
    """Return a validated IANA timezone name, warning + falling back on bad input."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        logger.warning("invalid %s=%r, falling back to %s", name, raw, default)
        return default
    return raw


def _list_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw or raw.lower() == "none":
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _enum_env(name: str, default: str, valid: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in valid:
        logger.warning("invalid %s=%r, falling back to %s", name, value, default)
        return default
    return value


def _https_url_env(name: str, default: str) -> str:
    """Return an https:// URL from env, falling back to default on empty
    or non-https values."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    if not raw.startswith("https://"):
        logger.warning("invalid %s=%r (not https), falling back to %s", name, raw, default)
        return default
    return raw


@dataclass(frozen=True)
class Settings:
    # NOTE: `slack_bot_token` is kept ONLY for `localtest.py`'s convenience —
    # it lets the local CLI build a real WebClient when an operator wants to
    # exercise Slack-reading tools. The Lambda runtime path does NOT use this
    # field; per-app bot tokens are looked up from SSM Parameter Store via
    # `src.credentials.CredentialsStore` keyed on `api_app_id`. Leaving this
    # field empty in production is correct.
    slack_bot_token: str
    llm_provider: str
    llm_model: str
    image_provider: str
    image_model: str
    agent_max_steps: int
    response_language: str
    dynamodb_table_name: str
    aws_region: str
    ssm_params_prefix: str = "/gurumi-bot/apps"
    ssm_cache_ttl_seconds: int = 300
    allowed_channel_ids: list[str] = field(default_factory=list)
    allowed_channel_message: str = ""
    allowed_user_ids: list[str] = field(default_factory=list)
    allowed_user_message: str = ""
    max_len_slack: int = 3000
    max_throttle_count: int = 100
    max_history_chars: int = 4000
    max_output_tokens: int = 4096
    bot_cursor: str = ":robot_face:"
    system_message: str | None = None
    persona_message: str | None = None
    tavily_api_key: str | None = None
    xai_api_key: str | None = None
    log_level: str = "INFO"
    default_timezone: str = "Asia/Seoul"
    max_doc_chars: int = 20_000
    max_doc_pages: int = 50
    max_doc_bytes: int = 25 * 1024 * 1024
    max_web_chars: int = 8000
    max_web_bytes: int = 2 * 1024 * 1024
    max_web_links: int = 20
    # Cap for `attach_image_from_url` — external image downloads. Slack
    # itself accepts up to 1GB per upload, but bigger payloads inflate
    # Lambda time and burn bandwidth, so the practical default is 10MB.
    max_image_bytes: int = 10 * 1024 * 1024
    jina_reader_base: str = "https://r.jina.ai"

    @classmethod
    def from_env(cls) -> "Settings":
        llm_provider = _enum_env("LLM_PROVIDER", "openai", _VALID_PROVIDERS)
        image_provider = _enum_env(
            "IMAGE_PROVIDER",
            os.getenv("LLM_PROVIDER", "openai").strip().lower() or "openai",
            _VALID_PROVIDERS,
        )
        response_language = _enum_env("RESPONSE_LANGUAGE", "ko", _VALID_LANGUAGES)
        system_message = os.getenv("SYSTEM_MESSAGE", "").strip() or None
        persona_message = os.getenv("PERSONA_MESSAGE", "").strip() or None
        tavily_key = os.getenv("TAVILY_API_KEY", "").strip() or None
        xai_key = os.getenv("XAI_API_KEY", "").strip() or None
        return cls(
            slack_bot_token=os.getenv("SLACK_BOT_TOKEN", "").strip(),
            llm_provider=llm_provider,
            llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
            image_provider=image_provider,
            image_model=os.getenv("IMAGE_MODEL", "gpt-image-1").strip(),
            agent_max_steps=_int_env("AGENT_MAX_STEPS", 6, minimum=2),
            response_language=response_language,
            dynamodb_table_name=os.getenv("DYNAMODB_TABLE_NAME", "lambda-gurumi-bot-dev").strip(),
            aws_region=os.getenv("AWS_REGION", "us-east-1").strip(),
            ssm_params_prefix=os.getenv("SSM_PARAMS_PREFIX", "/gurumi-bot/apps").strip() or "/gurumi-bot/apps",
            ssm_cache_ttl_seconds=_int_env("SSM_CACHE_TTL_SECONDS", 300, minimum=10),
            allowed_channel_ids=_list_env("ALLOWED_CHANNEL_IDS"),
            # Empty env var falls back to the Korean default — silent block
            # is confusing to end users (looks like the bot is broken). Set
            # the env var explicitly to a non-empty string to override.
            allowed_channel_message=(
                os.getenv("ALLOWED_CHANNEL_MESSAGE", "").strip()
                or "질문은 {} 채널을 이용해 주세요~"
            ),
            allowed_user_ids=_list_env("ALLOWED_USER_IDS"),
            allowed_user_message=(
                os.getenv("ALLOWED_USER_MESSAGE", "").strip()
                or "허용된 유저만 응답합니다."
            ),
            max_len_slack=_int_env("MAX_LEN_SLACK", 2000, minimum=500),
            max_throttle_count=_int_env("MAX_THROTTLE_COUNT", 100, minimum=1),
            max_history_chars=_int_env("MAX_HISTORY_CHARS", 4000, minimum=500),
            max_output_tokens=_int_env("MAX_OUTPUT_TOKENS", 4096, minimum=256),
            bot_cursor=os.getenv("BOT_CURSOR", ":robot_face:").strip() or ":robot_face:",
            system_message=system_message,
            persona_message=persona_message,
            tavily_api_key=tavily_key,
            xai_api_key=xai_key,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            default_timezone=_tz_env("DEFAULT_TIMEZONE", "Asia/Seoul"),
            max_doc_chars=_int_env("MAX_DOC_CHARS", 20_000, minimum=1000),
            max_doc_pages=_int_env("MAX_DOC_PAGES", 50, minimum=1),
            max_doc_bytes=_int_env("MAX_DOC_BYTES", 25 * 1024 * 1024, minimum=64 * 1024),
            max_web_chars=_int_env("MAX_WEB_CHARS", 8000, minimum=500),
            max_web_bytes=_int_env("MAX_WEB_BYTES", 2 * 1024 * 1024, minimum=64 * 1024),
            max_web_links=_int_env("MAX_WEB_LINKS", 20, minimum=0),
            max_image_bytes=_int_env(
                "MAX_IMAGE_BYTES", 10 * 1024 * 1024, minimum=64 * 1024
            ),
            jina_reader_base=_https_url_env("JINA_READER_BASE", "https://r.jina.ai"),
        )
