# 확장 가이드

새 tool 또는 새 LLM provider 를 추가하는 방법을 step-by-step 으로 설명합니다. 둘 다 "파일 하나 추가 + `__init__.py` 한 줄" 수준으로 끝납니다 — agent 루프나 레지스트리 코드는 건드리지 않습니다.

> 새 **reaction handler** (예: `:thumbsup:`로 별도 동작)를 추가하려면 [docs/reactions.md](reactions.md)의 "새 reaction handler 추가" 섹션을 보세요. 패턴은 비슷합니다 — 함수 한 개 + `REACTION_HANDLERS` dict 한 줄.

---

## 새 tool 추가

Tool 은 LLM 이 native function calling 으로 호출하는 Python 함수입니다. `@tool(...)` 데코레이터가 JSON Schema · 레지스트리 등록 · dispatch 테이블을 한 번에 처리합니다.

### 1. 파일 만들기

예: 랜덤 정수를 반환하는 `random_int` tool.

`src/tools/random_int.py`

```python
"""Random integer tool — demonstrates the minimum shape of a new tool."""
from __future__ import annotations

import random
from typing import Any

from src.tools.registry import ToolContext, default_registry, tool


@tool(
    default_registry,
    name="random_int",
    description="Return a random integer in the inclusive range [low, high].",
    parameters={
        "type": "object",
        "properties": {
            "low": {"type": "integer"},
            "high": {"type": "integer"},
        },
        "required": ["low", "high"],
    },
)
def random_int(ctx: ToolContext, low: int, high: int) -> dict[str, int]:
    if low > high:
        raise ValueError("low must be <= high")
    return {"value": random.randint(low, high)}
```

**규칙**

- 첫 번째 인자는 항상 `ctx: ToolContext` — LLM 에는 노출되지 않는 런타임 컨텍스트 (`slack_client`, `channel`, `thread_ts`, `event`, `settings`, `llm`, `user_id`). per-user 동작이 필요하면 `ctx.user_id` 사용 (`remember`/`forget` 참고).
- 나머지 인자는 `parameters` JSON Schema 가 그대로 정의. 키워드 전달되므로 LLM 이 부르는 argument 이름과 정확히 일치시킬 것.
- 반환 타입은 JSON 직렬화 가능해야 합니다 (`dict`, `list`, `str`, `int`, `float`, `bool`). Agent 루프가 이걸 `role=tool` 메시지로 다시 LLM 에 넘겨줍니다.
- 에러 처리: 복구 불가능한 실패는 `ValueError` 로 raise 합니다. `ToolExecutor` 가 잡아서 `{"ok": False, "error": "..."}` 로 감싸 LLM 이 상황을 보고 재계획할 수 있게 해줍니다.
- 외부 네트워크 I/O 가 있으면 host allowlist 또는 `_validate_public_https_url` 같은 SSRF 가드를 반드시 적용할 것 (`src/tools/web.py` 참고).

### 2. `__init__.py` 에 등록

`src/tools/__init__.py` 의 side-effect import 블록에 새 모듈 이름을 추가합니다.

```python
from . import (  # noqa: F401  (imported for side effects)
    image,
    memory,
    random_int,   # ← 추가
    search,
    slack,
    time,
    web,
)
```

`default_registry` 는 module import 시점에 데코레이터 호출로 채워지므로, 이 import 한 줄로 agent 가 새 tool 을 자동 인식합니다.

### 3. 테스트 작성

`tests/tools/test_random_int.py`

```python
"""Tests for src.tools.random_int."""
from __future__ import annotations

import pytest

from tests.tools._helpers import _ctx
from src.tools.random_int import random_int


def test_random_int_returns_value_in_range():
    ctx = _ctx()
    for _ in range(20):
        out = random_int(ctx, low=1, high=3)
        assert out["value"] in {1, 2, 3}


def test_random_int_rejects_inverted_range():
    ctx = _ctx()
    with pytest.raises(ValueError, match="low must be"):
        random_int(ctx, low=5, high=1)
```

공용 fixture (`_ctx`, `_settings`, `_streamed_read`) 는 `tests/tools/_helpers.py` 에서 import 하세요 — 각 테스트 파일에 중복 정의하지 않습니다.

### 4. `default_registry` 등록 검증

`tests/tools/test_registry.py` 의 `test_default_registry_has_expected_tools` 가 새 tool 이름을 assert 하도록 업데이트:

```python
def test_default_registry_has_expected_tools():
    names = set(default_registry.names())
    assert "random_int" in names   # ← 추가
```

### 5. (선택) 환경 변수 / 설정

새 tool 이 설정값을 읽어야 하면 `src/config.py` 의 `Settings` 에 필드를 추가하고 `from_env()` 에서 env 를 읽어오세요. 기본값과 최소값 검증은 `_int_env`, `_tz_env`, `_enum_env`, `_https_url_env` 헬퍼 패턴을 따릅니다. `.env.example` 에도 새 변수를 예시와 함께 추가하세요.

### 6. 문서 업데이트

- `README.md` 의 "Tools" 목록과 "환경 변수" 표에 추가.
- 비자명한 동작(SSRF 가드 · 동기화 이슈 · 외부 서비스 의존)이 있으면 [`docs/architecture.md`](architecture.md) 의 관련 섹션에 짧게 노트. invariant 수준으로 강한 규칙(깨면 회귀)이면 [`CLAUDE.md`](../CLAUDE.md) 의 "코드 변경 시 깨지기 쉬운 것들" 에도 추가.

---

## 새 LLM provider 추가

Provider 는 `LLMProvider` Protocol 을 구현하는 클래스입니다. 텍스트 chat · streaming chat · 이미지 describe · 이미지 generate · 이미지 edit 다섯 메서드를 만족해야 합니다. `_OpenAICompatProvider` 를 상속하면 네 메서드가 자동 제공되고 `edit_image` 도 OpenAI SDK `images.edit()` 기반의 기본 구현이 따라옵니다 — 다른 wire 형식이 필요한 provider 만 오버라이드합니다 (예: `XAIProvider.edit_image` 가 raw urllib 로 `/v1/images/edits` JSON POST). 이미지 편집을 지원하지 않는 provider 는 `edit_image` 에서 `NotImplementedError` 를 raise 하면 됩니다 — `ToolExecutor` 가 잡아 LLM 에 복구 가능한 에러로 surface 합니다.

### 1. 파일 만들기

예: 가상의 `MistralProvider`.

`src/llms/mistral.py`

```python
"""MistralProvider — OpenAI-wire compatible via https://api.mistral.ai/v1."""
from __future__ import annotations

from src.llms.openai_wire import _OpenAICompatProvider


class MistralProvider(_OpenAICompatProvider):
    """Mistral 의 chat completions API 는 OpenAI wire 와 호환되므로
    `_OpenAICompatProvider` 를 재사용합니다 (xAI 와 같은 패턴).

    `_OpenAICompatProvider.__init__(model, image_model, api_key)` 를 그대로
    쓰고, `BASE_URL` · `API_KEY_ENV_VAR` 클래스 속성으로 엔드포인트와 키 환경
    변수 이름만 덮어씁니다. 고유 request 변형이 필요하면 `_token_params` /
    `_image_generate_kwargs` 를 오버라이드하세요.
    """

    BASE_URL = "https://api.mistral.ai/v1"
    API_KEY_ENV_VAR = "MISTRAL_API_KEY"
```

OpenAI wire 호환이 아닌 provider (예: Bedrock 처럼 고유 SDK) 는 `LLMProvider` Protocol 다섯 메서드를 직접 구현해야 합니다. `src/llms/bedrock.py` 가 좋은 reference 입니다.

### 2. `factory.py` 에 분기 추가

`get_llm` 은 provider 문자열에 따라 인스턴스를 만드는 작은 팩토리입니다. API key 는 단일 `api_keys: dict[str, str | None]` 인자로 전달되므로, 새 provider 는 그 dict 에서 자신의 키를 꺼내 씁니다.

`src/llms/factory.py`

```python
from src.llms.mistral import MistralProvider   # ← 추가


def get_llm(
    provider: str,
    model: str,
    image_provider: str,
    image_model: str,
    region: str = "us-east-1",
    api_keys: dict[str, str | None] | None = None,
) -> LLMProvider:
    api_keys = api_keys or {}

    def build(p: str) -> LLMProvider:
        if p == "bedrock":
            return BedrockProvider(model=model, image_model=image_model, region=region)
        if p == "xai":
            return XAIProvider(model=model, image_model=image_model, api_key=api_keys.get("xai"))
        if p == "mistral":   # ← 추가
            return MistralProvider(model=model, image_model=image_model, api_key=api_keys.get("mistral"))
        return OpenAIProvider(model=model, image_model=image_model)

    text = build(provider)
    if image_provider == provider:
        return text
    return _CompositeProvider(text=text, image=build(image_provider))
```

### 3. Settings 에 API key 필드

`src/config.py`

```python
@dataclass(frozen=True)
class Settings:
    ...
    mistral_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        ...
        mistral_key = os.getenv("MISTRAL_API_KEY", "").strip() or None
        return cls(
            ...,
            mistral_api_key=mistral_key,
        )
```

### 4. `_VALID_PROVIDERS` 확장

같은 파일의 enum 상수에 `"mistral"` 을 추가합니다. 그래야 `LLM_PROVIDER=mistral` 이 enum 검증을 통과합니다.

```python
_VALID_PROVIDERS = {"openai", "bedrock", "xai", "mistral"}
```

### 5. `get_llm` 호출부 업데이트

Lambda 런타임은 `src/runtime.py::_get_llm()`에서, 로컬 CLI는 `localtest.py`에서 `get_llm`을 호출합니다. 둘 다 `api_keys` dict에 provider-key 쌍을 담아 넘깁니다 — 새 provider를 위해 dict에 항목 추가.

```python
# src/runtime.py / localtest.py — 기존
llm = get_llm(
    provider=settings.llm_provider,
    model=settings.llm_model,
    image_provider=settings.image_provider,
    image_model=settings.image_model,
    region=settings.aws_region,
    api_keys={"xai": settings.xai_api_key},
)

# ↓ Mistral 지원 추가 후
llm = get_llm(
    provider=settings.llm_provider,
    model=settings.llm_model,
    image_provider=settings.image_provider,
    image_model=settings.image_model,
    region=settings.aws_region,
    api_keys={
        "xai": settings.xai_api_key,
        "mistral": settings.mistral_api_key,   # ← 추가
    },
)
```

`OPENAI_API_KEY` 는 OpenAI Python SDK 가 환경 변수에서 직접 읽으므로 `api_keys` dict 에 넣지 않습니다 — 환경 변수만 설정하면 됩니다. Bedrock 도 AWS SDK 의 credential chain 을 사용하므로 별도 key 전달이 불필요합니다.

### 6. 테스트 작성

`tests/llms/test_mistral.py`

```python
"""Tests for src.llms.mistral."""
from __future__ import annotations

from src.llms.mistral import MistralProvider


def test_mistral_provider_exposes_mistral_base_url():
    assert MistralProvider.BASE_URL == "https://api.mistral.ai/v1"
    assert MistralProvider.API_KEY_ENV_VAR == "MISTRAL_API_KEY"


def test_mistral_provider_constructs_openai_client_with_base_url():
    provider = MistralProvider(
        model="mistral-large",
        image_model="",
        api_key="test-key",
    )
    client = provider._get_client()
    assert str(client.base_url).startswith("https://api.mistral.ai/")
```

OpenAI-wire 호환 provider 는 `_OpenAICompatProvider` 테스트 패턴을 참고 (`tests/llms/test_xai.py` 가 좋은 예시).

### 7. 문서 · `.env.example` 업데이트

- `.env.example` 에 `MISTRAL_API_KEY=""` 추가.
- `README.md` 의 "환경 변수" 표 · "모델 매트릭스" 에 Mistral 컬럼 또는 행 추가.

---

## 공통 체크리스트

작업이 끝났다 싶을 때 한 번 확인:

- [ ] 전체 테스트 통과 (`python -m pytest`).
- [ ] `python -c "from src.tools import default_registry; print(sorted(default_registry.names()))"` 에 새 tool 이름이 나타남.
- [ ] (provider 추가 시) `python -c "from src.config import Settings; from src.llms import get_llm; get_llm(provider='<name>', ...)"` 가 에러 없이 객체를 반환.
- [ ] `.env.example` 에 새 env 가 반영됨.
- [ ] `README.md` 의 tool/provider 목록과 환경 변수 표가 업데이트됨.

이 패턴은 agent 루프 (`src/agent.py`), 레지스트리 내부 (`src/tools/registry.py`), 기존 provider/tool 의 코드를 건드리지 않도록 설계됐습니다. 그 파일들을 열어야 하는 변경은 확장 범위를 벗어난 것이므로 별도 논의/설계가 필요합니다.
