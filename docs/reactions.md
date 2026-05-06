# Reactions

봇이 작성한 메시지에 특정 이모지로 반응하면 봇이 자기 메시지에 동작을 수행할 수 있습니다. 현재 구현된 reaction:

| 이모지 | 동작 | 권한 |
|--------|------|------|
| `:x:` | 봇 메시지 삭제 (`chat.delete`) | 원 질문자 OR `ALLOWED_USER_IDS` 유저 |

## 동작 흐름

```
Slack reaction_added 이벤트
  ↓
router._on_reaction_added (Bolt receiver)
  ├─ pre-filter: REACTION_HANDLERS 키에 있는 reaction인지 + item.type == "message"인지
  └─ ack() → _enqueue_worker(...)
  ↓
router._process_worker (worker self-invoke)
  └─ event["type"] == "reaction_added" → handlers.reactions._process_reaction
  ↓
handlers.reactions._process_reaction (공통 dispatcher)
  ├─ item.type 재확인
  ├─ event_ts 기반 dedup (Slack/Lambda 재전송 흡수)
  └─ REACTION_HANDLERS[reaction] dispatch
  ↓
_handle_reaction_x_delete (개별 핸들러)
  ├─ 봇 메시지인지 확인 (item_user == auth.test().user_id)
  ├─ 권한 확인 (원 질문자 OR ALLOWED_USER_IDS)
  └─ chat.delete
```

Receiver의 pre-filter와 worker의 dispatch가 **같은 `REACTION_HANDLERS` dict**를 참조합니다 — 새 reaction을 등록하면 receiver path가 자동으로 열립니다.

## `:x:` 권한 모델

봇 메시지에 `:x:` reaction이 달렸을 때 다음 두 조건 중 하나라도 만족하면 메시지가 삭제됩니다:

1. **원 질문자**: 봇이 답한 thread를 시작한 사용자. 두 단계 lookup으로 결정:
   - `conversations.history(latest=msg_ts, oldest=msg_ts, inclusive=True, limit=1)` → 봇 메시지를 가져와 그 `thread_ts` 필드(원 질문 ts)를 추출
   - `conversations.replies(ts=parent_ts, limit=1)` → thread root parent 메시지의 `user`가 원 질문자
   - 한 번의 `conversations.replies(ts=봇답변ts)` 호출로는 안 됨 — Slack은 thread reply의 ts를 valid lookup key로 인정하지 않아 빈 결과를 반환합니다.
2. **`ALLOWED_USER_IDS`에 있는 유저**: per-app override → 글로벌 env var 순서로 resolve.

### 두 조건의 OR

자기 질문에 대한 봇 답변은 자기가 지울 수 있고, 운영자(ops user)는 누구의 질문이든 부적절한 봇 답변을 지울 수 있습니다.

### `:x:` 외 이모지는 무시

receiver pre-filter가 `REACTION_HANDLERS` 키와 비교 → 등록되지 않은 reaction은 worker invoke가 안 됩니다 (Lambda 비용 절약). worker도 defense-in-depth로 같은 체크를 합니다.

### 봇 메시지가 아닐 때

`item_user`(reaction 대상 메시지 작성자)와 `auth.test().user_id`(이 봇)를 비교해서 다르면 즉시 무시. 다른 봇 또는 사람의 메시지에 `chat.delete`를 시도하면 403 — 미리 차단해서 로그가 깔끔하게 유지됩니다.

`item_user`가 payload에서 누락된 경우(드물지만)에는 체크를 건너뛰고 `chat.delete` 자체가 enforce하게 둡니다.

### `conversations.history` / `conversations.replies` 실패 시

두 단계 lookup 중 어느 쪽이든 실패해도(missing scope, network) `ALLOWED_USER_IDS` 체크는 그대로 진행됩니다. 즉:

- ops user는 영향 없음
- 일반 유저는 자기 질문에 대한 답변도 지울 수 없게 됨 (fail-closed)

`reaction.unauthorized` 로그에는 `original_asker`(빈 문자열이면 `(lookup_failed)`)와 `parent_ts`가 함께 기록되므로, scope 누락으로 lookup이 실패하는지 진단하기 쉽습니다.

### `ALLOWED_USER_IDS = []`의 의미 (메시지 흐름과 다름)

| 흐름 | `[]`의 의미 |
|------|------------|
| **메시지 흐름** (`handlers.message`) | "이 앱은 모든 유저에게 열려 있음" — 누구나 봇과 대화 가능 |
| **Reaction 흐름** (`handlers.reactions`) | "원 질문자만 권한, 추가 ops 유저 없음" — 추가 권한자 없음 |

같은 attribute가 흐름에 따라 다르게 해석됩니다. 메시징은 user-facing surface(기본 개방), reaction-delete는 privileged action(기본 폐쇄)이기 때문에 의도된 비대칭입니다. 자세한 설계 근거는 [docs/architecture.md의 "메시지 흐름 vs reaction 흐름의 [] 의미 차이"](architecture.md#메시지-흐름-vs-reaction-흐름의--의미-차이)를 보세요.

### Silent delete

권한 없는 reactor는 **아무 응답도 받지 않습니다** — 차단 메시지를 보내지 않습니다. 봇의 존재나 reaction-delete 기능을 외부에 노출하지 않기 위함. 운영자는 `reaction.unauthorized` 로그로 모니터링.

## Slack 앱 설정 — 필요한 권한

reaction 기능을 켜려면 Slack 앱 콘솔에서 추가 설정이 필요합니다.

### OAuth & Permissions → Bot Token Scopes

기존(메시지 처리용)에 더해 추가:

- **`reactions:read`** — 필수. `reaction_added` 이벤트 수신
- **`channels:history`** — public 채널에서 동작 시
- **`groups:history`** — private 채널에서 동작 시
- **`im:history`** — DM에서 동작 시 (봇과의 1:1 DM은 보통 적용 안 됨)
- **`mpim:history`** — multi-person IM에서 동작 시

`*:history` scope는 `conversations.replies`로 thread parent를 조회하기 위함. 봇이 동작하는 채널 종류에 맞게 하나 이상 추가하세요.

`chat:write`는 이미 메시지 전송용으로 있을 것 — 그게 자기 메시지 `chat.delete`에도 사용됩니다.

### Event Subscriptions → Subscribe to bot events

추가:

- **`reaction_added`** — 필수

기존(`app_mention`, `message.im`)은 그대로 유지.

### 워크스페이스 재인증

scope 변경 후 봇이 동작하려면:

1. Slack 앱 콘솔에서 "Reinstall to Workspace" 클릭
2. 새 권한을 승인
3. 새 `bot_token`이 발급되면 SSM 업데이트:
   ```bash
   python scripts/apps.py set A0123ABC
   ```
4. `SSM_CACHE_TTL_SECONDS` 내 모든 warm Lambda 컨테이너에 반영

## 새 reaction handler 추가

3단계로 새 reaction을 등록할 수 있습니다.

### 1. 핸들러 함수 작성

`src/handlers/reactions.py`에 다음 시그니처로 함수 추가:

```python
def _handle_reaction_<name>(event: dict, client: WebClient, api_app_id: str) -> None:
    """<reaction>: 동작 설명.

    Authorization: <누가 호출 가능한지>
    """
    # 공통 dispatcher가 이미 처리한 것:
    # - set_request_id (uuid)
    # - item.type == "message" 필터
    # - event_ts 기반 dedup
    #
    # 핸들러는 다음에만 집중:
    # - 대상 메시지 검증
    # - 권한 확인
    # - 실제 동작
    ...
```

핵심 객체:

- `event`: Slack reaction_added payload
  - `event["item"]["channel"]`, `event["item"]["ts"]` — 대상 메시지 위치
  - `event["user"]` — reaction을 누른 사람
  - `event["item_user"]` — 대상 메시지 작성자 (옵셔널)
  - `event["reaction"]` — reaction 이름 (예: `"x"`, `"thumbsup"`)
  - `event["event_ts"]` — firing당 unique
- `client`: 이 앱의 Slack WebClient (token은 worker가 SSM에서 resolve)
- `api_app_id`: per-app override resolution용 (`runtime._get_app_metadata().record(...)`)

런타임 헬퍼:

- `runtime._get_bot_user_id(client, api_app_id)` — 캐시된 `auth.test().user_id` (봇 메시지 검증용)
- `runtime._get_app_metadata().record(...)` — per-app override 행 가져오기 (`ALL_NEW` 라서 별도 GetItem 불필요)
- `runtime.settings.allowed_user_ids` — 글로벌 env var fallback

예시 — 봇 메시지에 `:thumbsup:` 누르면 로그만 (실제 동작 없음):

```python
def _handle_reaction_thumbsup_log(event: dict, client: WebClient, api_app_id: str) -> None:
    """`:thumbsup:` → log positive feedback (no user-visible action)."""
    item = event.get("item") or {}
    bot_user_id = runtime._get_bot_user_id(client, api_app_id)
    if not bot_user_id or event.get("item_user") != bot_user_id:
        return
    log_event(
        runtime.logger,
        "reaction.feedback_positive",
        reactor=event.get("user", ""),
        channel=item.get("channel"),
        ts=item.get("ts"),
        api_app_id=api_app_id,
    )
```

### 2. `REACTION_HANDLERS` dict에 등록

같은 파일 하단 dict에 한 줄 추가:

```python
REACTION_HANDLERS: dict[str, "callable"] = {
    "x": _handle_reaction_x_delete,
    "thumbsup": _handle_reaction_thumbsup_log,   # ← 추가
}
```

이걸로 receiver pre-filter도 자동으로 열립니다. 별도 코드 변경 불필요.

### 3. 테스트 추가

`tests/test_handlers_reactions.py`에 케이스 추가. `_handle_reaction_x_delete`의 테스트 패턴이 좋은 reference:

```python
def test_handle_reaction_thumbsup_logs_positive_feedback(app_module, monkeypatch, caplog):
    _reset_bot_user_id_cache(app_module)
    monkeypatch.setattr(_runtime, "_get_dedup", lambda: _FakeDedup())
    monkeypatch.setattr(_runtime, "_get_app_metadata", lambda: _RecordingMetadata())

    client = _RecordingClient(bot_user_id="U-BOT")
    event = _reaction_event(reaction="thumbsup", item_user="U-BOT")
    
    import logging
    with caplog.at_level(logging.INFO, logger="app"):
        _reactions._process_reaction(event, client, api_app_id="A1")
    
    assert any("reaction.feedback_positive" in r.getMessage() for r in caplog.records)
    assert client.deleted == []   # 삭제는 없음
```

### Slack 앱 설정 (필요한 경우)

새 reaction이 새 scope를 요구하면 Slack 앱 콘솔에서 추가하고 워크스페이스 재인증.

## 설계 결정 메모

### 왜 dispatch table?

새 reaction = 새 함수 + dict 한 줄. dispatcher 코드(`_process_reaction`)는 변경 없음. 단순 케이스만 있으면 if/elif로도 충분하지만, dict가:

- receiver pre-filter와 worker dispatch가 **같은 source of truth**를 공유 (한 곳에 등록하면 양쪽 자동)
- 테스트에서 `monkeypatch.setitem(_reactions.REACTION_HANDLERS, ...)`로 임시 핸들러 주입 가능 — 실제 동작은 격리

### 왜 권한 체크는 핸들러 책임?

reaction마다 권한 모델이 다를 수 있습니다 (예: `:x:`는 폐쇄형, `:thumbsup:`는 누구나). 공통 dispatcher가 권한을 결정하면 한 가지 모델로 강제됩니다. 핸들러가 자기 정책을 owner — `_process_reaction`은 dedup·payload-shape 같은 *공통 인프라*만.

### 왜 silent delete?

권한 없는 reactor에게 차단 메시지를 보내면 봇의 존재 + 기능을 외부에 노출하게 됩니다. 운영자가 `reaction.unauthorized` 로그로 모니터링하는 게 더 안전.

### 왜 `:x:`?

여러 비슷한 X 이모지 (`:x:`, `:negative_squared_cross_mark:`, `:heavy_multiplication_x:`)가 있습니다. 가장 일반적인 `:x:`(빨간 X)만 트리거하도록 명시적 결정 — 비슷한 이모지를 모두 받으면 의도하지 않은 삭제 위험.

### 왜 `_get_bot_user_id` 캐시는 success-only?

`auth.test` 호출이 일시적으로 실패할 수 있음 (네트워크 등). 실패를 캐시하면 컨테이너가 죽기 전까지 모든 reaction 권한 체크가 skip됩니다 — 의미 있는 reaction이 무시되는 회귀. Success만 캐시 + failure는 매번 retry.

## 관련 문서

- [docs/architecture.md](architecture.md) — 멀티테넌트 모델, dedup, per-app override 전체 설계
- [docs/operations.md](operations.md) — 운영 CLI, ACL/persona 설정
- [docs/extending.md](extending.md) — 새 tool/provider 추가
