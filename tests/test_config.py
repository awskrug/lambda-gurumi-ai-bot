import pytest


@pytest.fixture
def reload_config():
    """Build a fresh Settings from the current os.environ (no module reload)."""

    def _reload():
        from src.config import Settings

        return Settings.from_env()

    return _reload


def _clear_env(monkeypatch):
    for key in [
        "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "LLM_PROVIDER", "LLM_MODEL",
        "IMAGE_PROVIDER", "IMAGE_MODEL", "OPENAI_API_KEY", "RESPONSE_LANGUAGE",
        "AGENT_MAX_STEPS", "DYNAMODB_TABLE_NAME", "AWS_REGION", "ALLOWED_CHANNEL_IDS",
        "ALLOWED_CHANNEL_MESSAGE", "ALLOWED_USER_IDS", "ALLOWED_USER_MESSAGE",
        "MAX_LEN_SLACK", "MAX_THROTTLE_COUNT",
        "MAX_HISTORY_CHARS", "BOT_CURSOR", "SYSTEM_MESSAGE", "PERSONA_MESSAGE", "TAVILY_API_KEY", "XAI_API_KEY", "LOG_LEVEL",
        "DEFAULT_TIMEZONE", "MAX_DOC_CHARS", "MAX_DOC_PAGES", "MAX_DOC_BYTES",
        "MAX_WEB_CHARS", "MAX_WEB_BYTES", "MAX_WEB_LINKS", "MAX_IMAGE_BYTES", "JINA_READER_BASE",
        "SSM_PARAMS_PREFIX", "SSM_CACHE_TTL_SECONDS",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_defaults(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    s = reload_config()
    assert s.llm_provider == "openai"
    assert s.llm_model == "gpt-4o-mini"
    assert s.image_provider == "openai"
    assert s.image_model == "gpt-image-1"
    assert s.response_language == "ko"
    assert s.agent_max_steps == 3
    assert s.max_len_slack == 2000
    assert s.allowed_channel_ids == []
    assert s.allowed_user_ids == []
    assert s.allowed_user_message == "허용된 유저만 응답합니다."
    assert s.tavily_api_key is None


def test_invalid_enum_falls_back(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("RESPONSE_LANGUAGE", "jp")
    monkeypatch.setenv("LLM_PROVIDER", "mystery")
    s = reload_config()
    assert s.response_language == "ko"
    assert s.llm_provider == "openai"


def test_invalid_int_falls_back_to_default(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENT_MAX_STEPS", "not-an-int")
    s = reload_config()
    assert s.agent_max_steps == 3


def test_int_below_minimum_clamped(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MAX_LEN_SLACK", "10")
    s = reload_config()
    assert s.max_len_slack == 500


def test_list_env_splits_commas(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "C1,C2, C3 ")
    s = reload_config()
    assert s.allowed_channel_ids == ["C1", "C2", "C3"]


def test_list_env_none_sentinel(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "None")
    s = reload_config()
    assert s.allowed_channel_ids == []


def test_allowed_user_ids_parsed_from_env(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_USER_IDS", "U1,U2, U3 ")
    monkeypatch.setenv("ALLOWED_USER_MESSAGE", "허용된 유저만 응답합니다.")
    s = reload_config()
    assert s.allowed_user_ids == ["U1", "U2", "U3"]
    assert s.allowed_user_message == "허용된 유저만 응답합니다."


def test_block_messages_default_to_non_empty_when_env_is_blank(monkeypatch, reload_config):
    """Empty env var must fall back to a non-empty Korean default — otherwise
    a blocked user gets no response and the bot looks broken. Operator can
    still override with an explicit non-empty value."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_CHANNEL_MESSAGE", "")
    monkeypatch.setenv("ALLOWED_USER_MESSAGE", "")
    s = reload_config()
    assert s.allowed_channel_message
    assert "{}" in s.allowed_channel_message  # has substitution placeholder
    assert s.allowed_user_message


def test_block_messages_explicit_override_wins(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_CHANNEL_MESSAGE", "Custom channel block")
    monkeypatch.setenv("ALLOWED_USER_MESSAGE", "Custom user block")
    s = reload_config()
    assert s.allowed_channel_message == "Custom channel block"
    assert s.allowed_user_message == "Custom user block"


def test_block_messages_whitespace_only_falls_back_to_default(monkeypatch, reload_config):
    """A whitespace-only env var (`   `) is meaningless intent — strip and
    fall back, same as empty."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_CHANNEL_MESSAGE", "   ")
    monkeypatch.setenv("ALLOWED_USER_MESSAGE", "   ")
    s = reload_config()
    assert s.allowed_channel_message
    assert "{}" in s.allowed_channel_message
    assert s.allowed_user_message


def test_ssm_params_prefix_default(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.delenv("SSM_PARAMS_PREFIX", raising=False)
    s = reload_config()
    assert s.ssm_params_prefix == "/gurumi-bot/apps"


def test_ssm_params_prefix_override(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SSM_PARAMS_PREFIX", "/myorg/slack")
    s = reload_config()
    assert s.ssm_params_prefix == "/myorg/slack"


def test_ssm_params_prefix_blank_falls_back_to_default(monkeypatch, reload_config):
    """Empty env var must not be propagated as the prefix — that would
    silently anchor SSM lookups at the root and produce confusing errors."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("SSM_PARAMS_PREFIX", "   ")
    s = reload_config()
    assert s.ssm_params_prefix == "/gurumi-bot/apps"


def test_ssm_cache_ttl_default(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.delenv("SSM_CACHE_TTL_SECONDS", raising=False)
    s = reload_config()
    assert s.ssm_cache_ttl_seconds == 300


def test_ssm_cache_ttl_clamped_to_minimum(monkeypatch, reload_config):
    """A pathologically small TTL would burn SSM API quota — clamp to
    the configured minimum (10s)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("SSM_CACHE_TTL_SECONDS", "1")
    s = reload_config()
    assert s.ssm_cache_ttl_seconds == 10


def test_persona_and_system_messages_default_none(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    s = reload_config()
    assert s.system_message is None
    assert s.persona_message is None


def test_persona_and_system_messages_from_env(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SYSTEM_MESSAGE", "Do not expose secrets.")
    monkeypatch.setenv("PERSONA_MESSAGE", "자연스러운 한국어로 핵심부터 답한다.")
    s = reload_config()
    assert s.system_message == "Do not expose secrets."
    assert s.persona_message == "자연스러운 한국어로 핵심부터 답한다."


def test_xai_provider_is_a_valid_enum_value(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "xai")
    monkeypatch.setenv("IMAGE_PROVIDER", "xai")
    s = reload_config()
    assert s.llm_provider == "xai"
    assert s.image_provider == "xai"


def test_xai_api_key_default_none_and_override(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    s = reload_config()
    assert s.xai_api_key is None

    monkeypatch.setenv("XAI_API_KEY", "xai-abc")
    s2 = reload_config()
    assert s2.xai_api_key == "xai-abc"


def test_doc_limits_defaults(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    s = reload_config()
    assert s.default_timezone == "Asia/Seoul"
    assert s.max_doc_chars == 20_000
    assert s.max_doc_pages == 50
    assert s.max_doc_bytes == 25 * 1024 * 1024


def test_default_timezone_fallback_on_invalid_env(monkeypatch, reload_config, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Narnia/Center")
    with caplog.at_level("WARNING"):
        s = reload_config()
    assert s.default_timezone == "Asia/Seoul"
    assert any("DEFAULT_TIMEZONE" in rec.message for rec in caplog.records)


def test_doc_limits_honor_env_and_clamp(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MAX_DOC_CHARS", "5000")
    monkeypatch.setenv("MAX_DOC_PAGES", "0")  # below minimum → clamps to 1
    monkeypatch.setenv("MAX_DOC_BYTES", "100")  # below minimum → clamps to 65536
    s = reload_config()
    assert s.max_doc_chars == 5000
    assert s.max_doc_pages == 1
    assert s.max_doc_bytes == 65_536


def test_default_timezone_custom_value(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    s = reload_config()
    assert s.default_timezone == "America/New_York"


def test_web_fetch_defaults(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    s = reload_config()
    assert s.max_web_chars == 8000
    assert s.max_web_bytes == 2 * 1024 * 1024
    assert s.max_web_links == 20
    assert s.jina_reader_base == "https://r.jina.ai"


def test_web_fetch_env_overrides(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MAX_WEB_CHARS", "1000")
    monkeypatch.setenv("MAX_WEB_BYTES", "131072")  # 128KB
    monkeypatch.setenv("MAX_WEB_LINKS", "5")
    monkeypatch.setenv("JINA_READER_BASE", "https://custom.reader.example")
    s = reload_config()
    assert s.max_web_chars == 1000
    assert s.max_web_bytes == 131072
    assert s.max_web_links == 5
    assert s.jina_reader_base == "https://custom.reader.example"


def test_web_fetch_below_minimum_clamped(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MAX_WEB_CHARS", "10")      # below 500 floor
    monkeypatch.setenv("MAX_WEB_BYTES", "1024")    # below 64KB floor
    s = reload_config()
    assert s.max_web_chars == 500
    assert s.max_web_bytes == 64 * 1024


def test_web_fetch_jina_reader_base_http_rejected(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("JINA_READER_BASE", "http://internal.proxy.example")
    s = reload_config()
    assert s.jina_reader_base == "https://r.jina.ai"  # fallback


def test_max_image_bytes_default(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    s = reload_config()
    assert s.max_image_bytes == 10 * 1024 * 1024


def test_max_image_bytes_env_override(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MAX_IMAGE_BYTES", "262144")  # 256KB
    s = reload_config()
    assert s.max_image_bytes == 262144


def test_max_image_bytes_below_minimum_clamped(monkeypatch, reload_config):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MAX_IMAGE_BYTES", "1024")  # below 64KB floor
    s = reload_config()
    assert s.max_image_bytes == 64 * 1024
