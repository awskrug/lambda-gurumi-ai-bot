# Architecture

이 문서는 lambda-gurumi-bot이 *왜* 이렇게 구성되었는지, 코드를 깊이 이해하려는 기여자를 위한 자료입니다. 표면적인 사용법은 [README.md](../README.md), AI agent를 위한 invariant 모음은 [CLAUDE.md](../CLAUDE.md)를 보세요.

## 큰 그림

```
┌────────────────┐  POST /slack/events
│ Slack workspace│──────────────────┐
└────────────────┘                  ▼
                ┌──────────────────────────────────────┐
                │ API Gateway → Lambda (app.py)        │
                │ ├─ X-Slack-Retry-Num early-return    │
                │ └─ src.router._route_request         │
                │     ├─ parse body → api_app_id       │
                │     ├─ SSM lookup (signing+token)    │
                │     └─ per-app cached Bolt App       │
                └──────────────┬───────────────────────┘
              receiver path    │   ack + lambda:Invoke (async, _worker=True)
                               ▼
                ┌──────────────────────────────────────┐
                │ Lambda async self-invoke             │
                │ src.router._process_worker           │
                │   ├─ kind == command?                │
                │   │   → handlers.commands            │
                │   ├─ event.type == reaction_added?   │
                │   │   → handlers.reactions           │
                │   └─ otherwise                       │
                │       → handlers.message._process    │
                └──────────────┬───────────────────────┘
                worker path    │
                ▼              ▼
        ┌────────────┐   ┌─────────────────────────────┐
        │ DynamoDB   │   │ src.agent.SlackMentionAgent │
        │ (5 prefix) │   │  ├─ LLM.chat(tools=spec)    │
        │            │   │  ├─ ToolExecutor.execute    │
        │            │   │  └─ loop ≤ AGENT_MAX_STEPS  │
        └────────────┘   └─────────────────────────────┘
                                          │
                            ┌─────────────┼─────────────┐
                            ▼             ▼             ▼
                     ┌──────────┐  ┌──────────┐  ┌────────────┐
                     │ OpenAI   │  │ Bedrock  │  │ Slack Web  │
                     │ Chat API │  │ Messages │  │ API (tools)│
                     │ Vision   │  │ Converse │  └────────────┘
                     └──────────┘  └──────────┘
```

## 모듈 분담

`app.py`는 Lambda entrypoint(`serverless.yml: handler: app.lambda_handler`)만 담당합니다. 진짜 로직은 `src/`에 있습니다.

| 모듈 | 책임 |
|------|------|
| `app.py` | `lambda_handler` — `_worker` 플래그/`X-Slack-Retry-Num` 헤더로 worker/receiver/retry 분기. `serverless.yml`의 deployment contract라서 위치/이름 변경 금지. |
| `src/runtime.py` | 프로세스 단위 싱글톤 (LLM·DDB·SSM·Lambda 클라이언트, Bolt 앱 캐시, bot user_id 캐시) + lazy accessors + `settings` + `logger`. Lambda 웜 컨테이너에서 모든 요청이 재사용. |
| `src/router.py` | Receiver path(parse → resolve creds → Bolt) + worker path(`_process_worker`가 event 타입에 따라 분기) + per-app Bolt 앱 캐시. |
| `src/handlers/message.py` | `app_mention` + DM `message` 이벤트 처리. allowlist + per-app override + agent 실행 + streaming + history 저장. |
| `src/handlers/reactions.py` | `_process_reaction` dispatcher + `REACTION_HANDLERS` dict + 개별 reaction handler (`:x:` → `chat.delete`, `:img-gpt:`/`:img-xai:` → 이미지 생성). |
| `src/handlers/commands.py` | `/img-gpt`·`/img-xai` slash command worker. 명령별 고정 이미지 provider/model(`IMAGE_MODEL_GPT`/`IMAGE_MODEL_XAI`)로 `generate_image` + `files_upload_v2`. 오류는 `response_url` ephemeral. |
| `src/agent.py` | Native function calling 기반 agent 루프. 4-phase 파이프라인(질문 → 의도·계획 → 툴 사용 → 응답)의 핵심. |
| `src/llms/` | LLM provider 패키지. Protocol + OpenAI/xAI/Bedrock/Upstage 구현. |
| `src/tools/` | Tool 패키지. `@tool` 데코레이터로 self-register. |
| `src/credentials.py` | SSM Parameter Store 기반 멀티테넌트 credentials 캐시 (positive + negative). |
| `src/app_metadata.py` | `app:{api_app_id}` DynamoDB 행 — 자동 등록되는 앱 레지스트리 + per-app override 저장. |
| `src/dedup.py` | DynamoDB 조건부 put 기반 중복 제거(`reserve`/`is_done`/`mark_done` 두 단계) + 스레드 대화 메모리. |
| `src/memory.py` | `MemoryStore` — `mem:{user_id}` 행에 사용자별 영속 메모리. TTL 없음. `remember`/`forget` 도구가 사용. |
| `src/slack_helpers.py` | 메시지 분할(코드펜스 인지) + 스트리밍 + 사용자 캐시 + thread status. |
| `src/logging_utils.py` | 구조화 JSON 로깅 + request_id 컨텍스트. |

### Cross-module 호출 규약 (load-bearing)

`src/router.py`, `src/handlers/message.py`, `src/handlers/reactions.py`, `src/handlers/commands.py`는 공유 상태를 다음 패턴으로만 접근합니다:

```python
# ✅ 모듈 객체 import + 속성 접근 (late binding)
from src import runtime
def some_handler(...):
    dedup = runtime._get_dedup()

# ❌ from import (early binding) — monkeypatch 무력화
from src.runtime import _get_dedup
def some_handler(...):
    dedup = _get_dedup()  # 테스트의 patch가 안 보임
```

이유: `from X import Y`는 import 시점에 `Y` 객체를 자기 namespace에 binding합니다. 이후 누군가가 `monkeypatch.setattr(src.runtime, "Y", fake)`를 해도, 이미 caller가 들고 있는 reference는 바뀌지 않습니다. 모듈 객체를 import한 뒤 속성으로 접근하면 매 호출마다 속성 lookup이 일어나 patch가 즉시 반영됩니다. 테스트 스위트의 절반 정도가 이 패턴에 의존합니다.

## 멀티테넌트 credential resolution

이 Lambda는 단일 배포로 여러 Slack 앱을 서빙합니다. 글로벌 `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` env var가 **없습니다** — 시크릿은 요청마다 SSM에서 조회합니다.

### SSM 경로

```
{SSM_PARAMS_PREFIX}/{api_app_id}/signing_secret  (SecureString)
{SSM_PARAMS_PREFIX}/{api_app_id}/bot_token       (SecureString)
```

기본 prefix: `/gurumi-bot/apps`. `SSM_PARAMS_PREFIX` env로 변경 가능.

### CredentialsStore 캐시

`src/credentials.py::CredentialsStore`가 in-process 캐시를 가진 GetParameters를 수행합니다. TTL 기본 5분(`SSM_CACHE_TTL_SECONDS`).

- **Positive 결과**: 정상 시크릿 캐시. 로테이션은 TTL 내 반영.
- **Negative 결과**: 시크릿 부재(미등록 앱)**도 캐시**. 미설정 앱이 트래픽을 보내도 SSM `GetParameters`를 매 요청 호출하지 않아 throttle quota를 보호.

### 시크릿 부재 시 동작

- HTTP 200 응답 (Slack의 자동 retry 폭주 방지 — 미설정은 영구 오류이므로 재시도 무의미)
- 구조화 로그에 `request.unknown_app` (receiver) / `worker.unknown_app` (worker) 기록
- 운영자는 Parameter Store에 SecureString을 추가해야 함 (자세한 절차는 [docs/operations.md](operations.md) 참조)

### 시그니처 검증의 시점

Receiver는 body를 *시그니처 검증 전*에 한 번 파싱합니다 — 단지 `api_app_id`를 뽑아 적절한 `signing_secret`을 SSM에서 가져오기 위해서입니다. 실제 시그니처 검증은 Bolt 안에서 일어나므로, 위조된 `api_app_id`는 잘못된(또는 없는) secret으로 resolve되어 거부됩니다.

`url_verification` 이벤트는 예외입니다 — body에 `api_app_id`가 없어 어떤 secret으로 검증할지 결정할 수 없습니다. Body가 단순한 `{type, token, challenge}`로 actionable payload가 없으므로 challenge를 그대로 echo하는 게 가장 깔끔한 해법입니다.

### Per-app Bolt App 캐시

`src/runtime.py::_bolt_apps`는 `api_app_id → ((signing_secret, bot_token), App)` 형태입니다. secret 튜플을 *value*에 둠으로써 로테이션을 감지할 수 있습니다 — `CredentialsStore`가 TTL 후 새 튜플을 반환하면 캐시 미스로 판단해 App을 재빌드, 새 `signing_secret`으로 시그니처 검증을 시작합니다. 이 패턴이 없으면 컨테이너가 죽기 전까지 옛 secret으로 검증해버립니다.

## Receiver / worker split

API Gateway HTTP 통합은 응답 30초 제한이 있습니다. 하지만 LLM 응답은 종종 그것을 넘깁니다. 그래서 receiver는 빠르게 ack하고, 실제 작업은 비동기 self-invoke로 위임합니다.

### Receiver path (HTTP)

1. `lambda_handler` — `X-Slack-Retry-Num` 헤더 있으면 200 OK 즉시 반환 (Slack 재시도 흡수)
2. `router._route_request` — path가 `/slack/command`로 끝나면 `_route_command`(form-urlencoded body에서 `api_app_id`만 추출), 아니면 JSON body parse → `api_app_id` → SSM credentials → 캐시된 Bolt App
3. Bolt — 시그니처 검증 → 등록된 핸들러(`_on_mention`, `_on_message`, `_on_reaction_added`, `@app.command` 핸들러) 디스패치
4. 핸들러 — `ack()` → `router._enqueue_worker(event, ...)` (slash command는 `_enqueue_command_worker`) → 즉시 return

전체 receiver 경로는 보통 수백 ms.

### Worker path (Lambda async self-invoke)

`router._enqueue_worker`는 `lambda:Invoke`로 자기 자신을 호출합니다 (`InvocationType=Event`, payload `{"_worker": True, "slack_event": ..., "is_dm": ..., "api_app_id": ...}`).

**시크릿은 페이로드에 절대 넣지 않습니다.** Lambda invoke payload는 CloudTrail 등 다양한 곳에 노출될 수 있으므로 `api_app_id`만 운반하고 worker가 다시 SSM에서 시크릿을 조회합니다.

`lambda_handler`는 `event["_worker"] is True`를 보고 worker 경로로 진입 → `router._process_worker` 호출 → payload에 따라:

- `kind == "command"` → `handlers.commands._process_command(...)` (slash command — payload는 `{"_worker": True, "kind": "command", "command_payload": {...}, "api_app_id": ...}`)
- `event["type"] == "reaction_added"` → `handlers.reactions._process_reaction(...)`
- 그 외 → `handlers.message._process(...)`

Worker는 Lambda의 300초 budget(`serverless.yml: timeout: 300`)을 모두 씁니다. 도구 timeout (예: `generate_image` 240초)는 compose + upload + history-save가 남은 budget에 들어가도록 잡혀 있습니다.

### Inline fallback (로컬 전용)

`AWS_LAMBDA_FUNCTION_NAME` env가 없는 *로컬/테스트* 환경에서는 `_enqueue_worker`가 `_process_worker`를 inline 실행합니다 — receiver/worker 경계가 없는 단일 프로세스라 안전.

Lambda 환경에서 `lambda.invoke`가 raise하면 inline 실행을 *하지 않습니다*. receiver는 API Gateway 30s + Slack ack 3s 윈도우 안에 있고 inline agent run은 둘 다 초과해 retry 폭주를 유발하기 때문입니다. 대신 best-effort `chat_postMessage`로 "잠시 후 다시 시도" 안내를 보내고 drop. 운영자가 IAM/throttle/네트워크 등 invoke 실패 원인을 해결해야 합니다 (CloudWatch에 traceback 기록).

### Slash command 경로 (`/img-gpt`, `/img-xai`)

Slash command는 `application/x-www-form-urlencoded`로 별도 엔드포인트 `/slack/command`에 도착합니다. `_route_command`가 body에서 `api_app_id`만 추출(원본 body는 Bolt 시그니처 검증을 위해 유지)하고, `@app.command(...)` 핸들러가 `ack()` 후 `_enqueue_command_worker`로 위임합니다. Worker(`handlers.commands._process_command`)는:

- `_COMMAND_TO_IMAGE` 매핑으로 명령별 고정 provider/model 결정 (`/img-gpt` → OpenAI + `IMAGE_MODEL_GPT`, `/img-xai` → xAI + `IMAGE_MODEL_XAI`) — 배포 기본값 `IMAGE_PROVIDER`/`IMAGE_MODEL` 우회
- `trigger_id` 기반 두 단계 dedup (`dedup:cmd:{trigger_id}`) — 생성/업로드 실패 시 `mark_done`을 생략해 Lambda async retry가 재시도 가능
- 오류는 `response_url`로 ephemeral 응답 (호출자에게만 보임)

invoke 실패 시에도 inline 실행하지 않고 `response_url` ephemeral 안내 후 drop — `_enqueue_worker`와 같은 trade-off.

## DynamoDB — 단일 테이블, 다섯 prefix

`DYNAMODB_TABLE_NAME` 단일 테이블이 다섯 종류 행을 보관합니다:

| Key prefix | 의미 | TTL |
|-----------|------|-----|
| `dedup:{key}` | 처리 중(in-flight) 예약 | 5분 (Lambda timeout과 일치, `expire_at`) |
| `done:{key}` | 성공 처리 완료 마커 | 1시간 (`expire_at`) |
| `ctx:{thread_ts}` | 스레드 대화 메모리 | 1시간 (`expire_at`) |
| `app:{api_app_id}` | 앱 레지스트리 + per-app override | **없음** (영구) |
| `mem:{user_id}` | 사용자별 영속 메모리 (`remember`/`forget`) | **없음** (영구) |

DynamoDB TTL은 `expire_at` 속성이 *명시적으로 있는* 행만 만료시킵니다. `app:`/`mem:` 행에 `expire_at`을 실수로 추가하면 등록된 앱·사용자 메모리가 자동 삭제됩니다 — 절대 추가하지 마세요.

### 두 단계 dedup — `dedup:` (5분, Lambda timeout과 일치) + `done:` (1h)

워커 path 진입 순서:

1. `is_done(key)` 확인 → 이미 완료면 silent return
2. `reserve(key)` (300s TTL — Lambda function timeout과 일치) — 실패면 in-flight skip
3. agent.run + 응답 전송 + history persist
4. **응답 전송 + history persist 직후** `mark_done(key)` (1h `done:` 마커). history persist 는 try/except 로 감싸여 있어 실패해도 `mark_done` 은 건너뛰지 않는다

두 단계의 의미가 다릅니다:

- **`dedup:` (5분)** — *in-flight 보호*. 무거운 도구(`generate_image`/`edit_image`, 240s)가 실행 중인 동안에는 같은 payload의 동시 재전달을 차단합니다. TTL이 Lambda timeout과 같으므로 워커가 강제 종료된 직후엔 row가 만료되어 다음 async retry가 새 in-flight 슬롯을 잡을 수 있습니다.
- **`done:` (1시간)** — *영구 idempotency*. 정상 종료된 요청에 대해 한 시간 동안 모든 retry를 차단합니다.

Reaction은 별도 키 형태: `dedup:reaction:{event_ts}:{reactor}` / `done:reaction:...`. Slash command는 `dedup:cmd:{trigger_id}` (생성 실패 시 `mark_done` 생략 — retry가 재시도 가능). 모두 같은 두 단계 패턴.

### GSI: `user-index`

해시 `user`, range `expire_at`, projection `KEYS_ONLY`. 유저당 진행 중 요청 수를 세는 throttle (`count_user_active(user)`)에 사용. `MAX_THROTTLE_COUNT` (기본 100) 초과 시 `_process`가 거부.

## Slack/Lambda 재시도 — DynamoDB 조건부 put dedup

세 가지 재시도 경로가 모두 같은 dedup 키로 수렴합니다:

1. **Slack 재시도** — Slack의 3-attempt 재시도 스케줄. `lambda_handler`가 `X-Slack-Retry-Num` 헤더로 short-circuit (worker 호출 안 함)
2. **Lambda async 재시도** — async invoke 실패 시 Lambda의 기본 2회 재시도. 같은 `_worker` payload가 다시 실행됨
3. **우발적 재호출**

Worker path 첫 줄에서 `DedupStore.reserve(f"dedup:{client_msg_id}")`가 `put_item(ConditionExpression="attribute_not_exists(id)")`을 수행. 중복 키는 `ConditionalCheckFailedException` → False → silent return. `is_done` 선행 체크가 있어 retry는 `done:` 마커가 있으면 `reserve` 단계 전에 끝납니다(상세는 위 "두 단계 dedup" 섹션).

Get-then-put 패턴은 race가 있으므로 절대 그리로 바꾸지 마세요 — 동시 두 개의 worker가 같은 메시지를 처리할 수 있습니다.

## Per-app overrides (ACL + persona)

`app:{api_app_id}` 행의 세 속성이 매칭되는 글로벌 env var를 *덮어씁니다*:

| 속성 | 타입 | 덮어쓰는 env var |
|------|------|-----------------|
| `allowed_channel_ids` | list | `ALLOWED_CHANNEL_IDS` |
| `allowed_user_ids` | list | `ALLOWED_USER_IDS` |
| `persona_message` | string | `PERSONA_MESSAGE` |

### 3-state 계약

세 속성 모두 동일한 해석 (`handlers.message._process` 안의 `_effective` 헬퍼):

| 속성 상태 | 의미 |
|-----------|------|
| 속성 *없음* | 글로벌 env var 사용 (기본; 신규 앱) |
| 속성 = `[C1, C2]` / `"text"` | per-app 값 사용, 글로벌 무시 |
| 속성 = `[]` / `""` | 빈값을 *명시적 오버라이드*로 보존. 리스트 = "이 앱은 모두 허용", 문자열 = "이 앱은 페르소나 없음" |

빈 리스트/문자열을 "속성 없음"과 같은 의미로 collapse하면 override 의도를 잃습니다. 예: 글로벌이 restrictive `ALLOWED_USER_IDS`인데 한 앱만 모두 허용하고 싶을 때 `[]`로 표현 — 속성 부재로 collapse하면 글로벌이 다시 적용되어 의도와 반대 결과.

### 메시지 흐름 vs reaction 흐름의 `[]` 의미 차이

**의도된 비대칭**:

- **메시지 흐름** (`handlers.message`): `[]` = "이 앱은 모든 유저에게 열려 있음". 메시징은 기본 개방형이라서 빈 리스트 = 전부 허용.
- **Reaction 흐름** (`handlers.reactions`): `[]` = "원 질문자만 권한, 추가 ops 유저 없음". Reaction-delete는 권한 동작이라 기본 폐쇄. 빈 리스트 = 추가 권한자 없음 (원 질문자 체크는 별도 path).

같은 속성을 다르게 해석하는 이유는 메시징은 user-facing surface(기본 개방), reaction-delete는 privileged action(기본 폐쇄)이기 때문입니다.

### Resolution 비용

`AppMetadataStore.record(...)`가 `ReturnValues=ALL_NEW`로 update_item을 호출 → 응답에 전체 행이 포함. 그래서 metadata 기록 + override resolution이 **동일한 DynamoDB 라운드트립**에서 끝납니다. 별도 GetItem 없음.

DynamoDB read 실패 시 `record()`가 None 반환 → bot은 글로벌 env var로 fail-open. 일시적 DDB outage가 봇 전체를 막지 않도록 의도된 설계.

## Agent 루프 — 네이티브 function calling

`src/agent.py`는 `registry.specs()`를 `LLMProvider.chat(tools=...)`에 직접 전달합니다. Provider가 그것을 백엔드별 형식으로 번역:

- OpenAI/xAI: `tools=[{"type": "function", "function": {...}}]`
- Anthropic Claude: `tools=[{"name", "description", "input_schema"}]`
- Amazon Nova: `toolConfig.tools=[{...}]` via Converse API

**JSON-in-prompt parsing 없음** — tool calls는 구조화된 객체로 도착합니다.

### 중복 tool_call 억제

`_call_signature = name + sha1(args_json)`. 루프 내 같은 signature 반복 시 `{"ok": False, "error": "duplicate call skipped"}`로 short-circuit하고 LLM에 돌려줘 진행을 유도. 무한 retry 방지.

### 한 턴 내 병렬 tool 실행

LLM이 한 turn에 emit한 독립 `tool_calls`는 `ToolExecutor.execute_many`로 batch submit되어 worker pool(`max_workers=4`)에서 동시 실행됩니다. system prompt가 "independent tool들은 한 turn에 parallel로 emit하라"고 지시하므로(`fetch_thread_history` + `fetch_user_profile` + `read_attached_images` 같은 묶음이 전형) 실행 측도 직렬 합산이 아닌 max(latency)로 처리해야 hint가 의미를 가집니다.

- **순서 보존**: 결과는 입력 순서대로 반환되고 log/`on_step`/messages append도 원래 call 순서로 수행 — `tool_call_id` ↔ tool result 매칭과 관측성이 결정적.
- **중복 검사 우선**: pre-pass에서 signature 중복은 worker submit 없이 즉시 `duplicate call skipped`로 채움. 동일 turn에 same-sig가 둘 있으면 첫 번째만 실행.
- **per-call deadline**: 각 call의 timeout은 자기 submit 시점 기준으로 계산되어, 늦게 끝나는 sibling이 후속 call의 timeout 예산을 silent하게 늘리지 않음.

루프 종료: `not result.tool_calls`(LLM이 더 이상 도구를 요청하지 않음) 또는 `max_steps` 도달. max_steps 도달 시 `_compose_without_tools`가 `tools=None`으로 한 번 더 호출 — 사용자에게 미완 상태로 응답하지 않게.

### 4-phase 파이프라인 (절대 단축 금지)

```
질문 → 의도·계획 (LLM hop, native function calling) → 툴 사용 (반복) → 응답 (LLM hop)
```

"의도 파악"과 "계획"은 한 번의 LLM 호출입니다 — 응답이 두 정보(요청 해석 + 다음 tool_calls)를 동시에 담아 옵니다. 별도 intent classifier hop을 넣지 마세요. 단축 시 caption/후속 대응/에러 처리가 사라집니다.

키워드 휴리스틱(예: "그려" → 이미지)으로 우회 금지 — LLM이 메시지를 읽고 `tool_calls`로 의도를 표현합니다.

## LLM provider families

`LLMProvider` Protocol은 `chat`, `stream_chat`, `describe_image`, `generate_image`, `edit_image` 다섯 메서드. 네 구현:

- **`OpenAIProvider`**: 기본 OpenAI 엔드포인트. `_token_params`가 모델군에 따라 `max_tokens`(legacy chat) vs `max_completion_tokens`(gpt-5/o1/o3/o4 reasoning) 자동 선택.
- **`XAIProvider`**: `base_url="https://api.x.ai/v1"`, 명시적 `api_key`. OpenAI wire 호환이라 `_OpenAICompatProvider` 공유. Grok chat은 legacy `max_tokens + temperature` 조합 사용. 이미지 생성은 `size` 대신 `aspect_ratio`/`resolution`, 항상 `response_format="b64_json"`. **이미지 편집은 OpenAI SDK `images.edit()`가 미지원**(xAI 공식 문서 명시)이라 `urllib`로 `/v1/images/edits` 에 raw JSON POST — `image: {url, type}` 블록(단일은 객체, 다중은 배열).
- **`UpstageProvider`**: `base_url="https://api.upstage.ai/v1"`. OpenAI wire 호환이라 xAI처럼 `_OpenAICompatProvider` 공유 — Solar chat 모델은 legacy `max_tokens + temperature` 그대로 사용. **텍스트 전용** — `generate_image`/`edit_image`는 `NotImplementedError` raise. `IMAGE_PROVIDER`가 `LLM_PROVIDER`로 fallback하므로 `LLM_PROVIDER=upstage`에 `IMAGE_PROVIDER` 미지정이면 이미지 요청이 opaque한 API 에러 대신 명확한 메시지로 거부됩니다.
- **`BedrockProvider`**: 모델 family prefix로 내부 라우팅. Bedrock 직접 ID와 `us./eu./apac./global.` inference-profile variant 둘 다 인식.
  - `anthropic.claude*` → `invoke_model` + Messages API
  - `amazon.nova*` → `converse`/`converse_stream` + `toolConfig`
  - 미지정 → Claude path (no tools)
  - `edit_image`는 모든 family에서 `NotImplementedError` raise — Titan/Nova-Canvas/Stability 각각이 image-to-image에 다른 request 스키마를 쓰는데 검증된 코드 경로가 없어 silent 라우팅 대신 명시적 거부로 surface.

**`_CompositeProvider`**: 텍스트와 이미지 provider가 다른 경우 (예: OpenAI 텍스트 + Bedrock 이미지) wrap. `factory.get_llm`이 자동 빌드. `edit_image`도 `image` 쪽으로 위임 — 텍스트는 OpenAI, 이미지는 xAI 같은 조합에서 편집은 xAI로 라우팅됨.

### Image generation family routing

- Titan/Nova-Canvas → `TEXT_IMAGE` task
- Stability → `text_prompts`

`_build_image_body` 헬퍼 참조.

## Streaming

`OpenAIProvider.chat(on_delta=...)`가 `stream=True`로 전환 → 컨텐츠 delta를 forward. **`tool_calls` delta가 시작되면 forwarding 중단** — pre-tool 코멘터리("Let me search...")가 최종 응답에 누출되지 않도록.

`StreamingMessage`(`src/slack_helpers.py`):
- `append()`에서 `min_interval=0.6s` throttle
- buffer가 `MAX_LEN_SLACK` 근접 시 fresh `chat_postMessage`로 roll
- `stop()`이 oversized 최종 응답을 `MessageFormatter.split_message`로 분할

### Code-fence-aware split

`MessageFormatter.split_message`는 **문단 우선** greedy 분할이고, 코드펜스는 그 뒤의 *보정* 단계입니다:
1. `max_len` 안에서 마지막 `\n\n` (문단) 컷
2. 컷이 ` ``` ` 블록 안에 떨어지면(펜스 개수 홀수) 블록 직전 `\n\n`으로 재컷 — 블록이 `max_len`보다 크면 블록 안에서 `\n\n`/`\n`으로 자르고 `\n``` ` + ` ```\n`으로 닫고 연다
3. `max_len` 안에 `\n\n`이 아예 없으면 `_fallback_cut` — 문장 경계(`.!?` + 공백) → 단일 `\n` → hard slice

첫 chunk는 placeholder 메시지에 `chat.update`. 나머지는 `chat.postMessage(thread_ts=...)`로 새 메시지. `chat.update` 실패 시 (msg_too_long 등) 그 chunk도 새 메시지로 fallback.

## SSRF guards

### `fetch_webpage` (`src/tools/web.py`)

`_validate_public_https_url` 다단계 검증:
- `https` 스킴 강제
- IP literal 거부
- DNS 결과가 private/loopback/link-local/reserved/multicast/unspecified/non-global이면 거부 (CGNAT `100.64.0.0/10` 포함)

Jina Reader path (`{JINA_READER_BASE}/{percent-encoded url}`)가 실제 네트워크 hop을 수행. Raw fallback만 직접 fetch — `_NoRedirectHandler`로 3xx 거부 (302 → 사설 호스트 우회 차단). `MAX_WEB_BYTES`로 응답 크기 제한.

**DNS rebinding 한계**: pre-flight `getaddrinfo`와 실제 TCP connect는 별개 lookup. TTL=0 공격자가 IP를 바꿀 수 있음. Lambda는 VPC 밖이라 영향 제한적이지만, VPC/private-subnet egress 추가 시 재검토 필요.

### Slack file fetch (`src/tools/slack.py`, `src/tools/image.py`)

`read_attached_images`/`read_attached_document`/`edit_image` 등 Slack-hosted 파일을 받는 모든 도구는 모듈-내부 helper `_http_get(req, timeout)`을 거칩니다. helper는 `urllib.request.build_opener(_SlackRedirectHandler())`로 만든 opener를 사용 → **redirect는 `SLACK_IMAGE_HOSTS` 안에서만 허용**(Slack은 `url_private_download`에 대해 same-zone signed CDN URL refresh로 자주 302를 발급하므로 일률 거부할 수 없음). `SLACK_FILE_HOSTS` 밖으로 redirect되면 Authorization 헤더가 strip되어 봇 토큰이 cross-host로 leak되지 않습니다. 비-Slack 호스트로의 redirect는 `HTTPError("redirects not allowed (off-host)")`로 거부. `SLACK_FILE_HOSTS` allowlist도 동일 경로에서 강제 — 봇 토큰이 임의 URL fetch에 쓰이지 않도록.

크기 캡(`MAX_IMAGE_BYTES`/`MAX_DOC_BYTES`)은 `_read_body_capped`로 streamed read 단계에서 적용. Content-Length 검증과 read 도중 cap 둘 다.

### External image fetch (`attach_image_from_url`)

공개 HTTPS 이미지를 Slack에 첨부하는 경로는 5겹 방어:

1. `_validate_public_https_url` — DNS 검증 (`fetch_webpage`와 동일).
2. `_NoRedirectHandler` — 3xx 거부.
3. `_read_body_capped(MAX_IMAGE_BYTES)` — Content-Length + streamed cap.
4. Content-Type allowlist — 헤더가 `image/*`를 주장하지 않으면 즉시 거부.
5. `_detect_image_mime` — 응답 본문 선두(최대 12바이트) magic 시그니처 검증 (PNG/JPEG/GIF/WEBP/BMP). 헤더는 값싸게 거짓말할 수 있으므로 4단계만으로는 부족 — `Content-Type: image/png`로 위장한 HTML/SVG가 Slack preview 파이프라인에 들어가는 것을 막습니다.

## Structured logging + request_id

`src/logging_utils.py`가 root에 JSON handler 설치. `set_request_id(uuid)`가 각 worker 진입 시 호출됨. `log_event(logger, "agent.done", steps=..., tokens_in=...)`이 `extra_fields` dict를 JSON payload로 보존 → CloudWatch Insights 쿼리 친화적.

Python 3.12의 `LoggerAdapter.process()`가 `extra=`를 덮어쓰는 버그를 피하기 위해 `log_event`는 `logger.logger`(underlying `Logger`)로 dispatch.

로거 이름은 `"app"`로 통일 (`src/runtime.py`에서 한 번 생성). 모듈 분리 후에도 기존 CloudWatch 쿼리(`logger="app"`)와 호환.

## App 레지스트리 자동 구축

`src/app_metadata.py::AppMetadataStore.record()`가 dedup 통과 후 `app:{api_app_id}` 행을 lazy upsert. 첫 이벤트가 들어온 시점에 자동 등록. 별도 등록 절차 불필요.

운영자가 `scripts/apps.py list`로 등록된 앱을 조회 (자세한 사용법은 [docs/operations.md](operations.md)).

## 사용자별 영속 메모리

`src/memory.py::MemoryStore`가 `mem:{user_id}` 행에 사용자별 long-form 메모리를 저장합니다. 행 schema는 `entries` JSON blob (`{key: {value, ts}, ...}`).

**Scope**: 운영 가정상 user_id는 앱 간 unique. 같은 사람이 다른 Slack 앱으로 멘션해도 동일 user_id면 메모리가 따라갑니다. 이 가정이 깨지는 환경이라면 row id를 `mem:{api_app_id}:{user_id}`로 분리(코드 한 줄).

**Caps**: 항목 값 1000자, 사용자당 50개 키, blob 30KB. 초과 시 `remember`가 `ValueError`로 surface → LLM이 사용자에게 "메모리 가득" 안내.

**자동 주입 vs 도구**:
- agent 생성 시점에 `MemoryStore.get(user_id)`로 모든 entries를 한 번 로드 → system prompt의 별도 섹션("User memory:")에 렌더.
- LLM은 `remember(key, value)` / `forget(key)` 두 도구를 쓰며, **`recall` 도구는 없음** — 메모리는 이미 컨텍스트에 들어있어 round-trip이 무의미.
- 같은 turn에 `remember`로 저장한 값은 *그 turn의 system prompt에 다시 주입되지 않음*. 다음 turn부터 반영. self-confirming loop 방지.

## Config은 lazy

`Settings.from_env()`가 모듈 로드 시 실행되지만 Slack credentials는 검증하지 않습니다 — 멀티테넌트 모델에서 글로벌 Slack secret이 없으니 검증할 게 없습니다. `Settings.slack_bot_token`은 `localtest.py`가 Slack-reading tool을 CLI에서 시험할 때만 쓰는 *로컬 전용 편의 변수*이고, Lambda runtime은 절대 읽지 않습니다.

Enum/int 검증은 조용히 fallback + 경고:
- 잘못된 `LLM_PROVIDER=mystery` → `openai`
- `AGENT_MAX_STEPS=not-int` → `6`
- 최소값 미만은 clamp up

## Reaction 처리

별도 문서 [docs/reactions.md](reactions.md) 참조 — `:x:` 권한 모델, 이미지 생성 reaction(`:img-gpt:`/`:img-xai:`), 필요한 Slack scope, 새 reaction handler 추가 절차.

대상 메시지·스레드 resolution은 공통 헬퍼 `_lookup_reacted_message`가 담당합니다. Slack의 `conversations.replies`를 *reply* ts로 호출하면 전체 스레드가 아니라 그 reply 하나만(자신의 `thread_ts` 포함) 반환됩니다. 그래서: ① 1차 `conversations.replies(ts=대상ts)` (실패 시 `conversations.history(latest=ts, inclusive, limit=1)` fallback — 반환 ts가 정확히 일치할 때만 수용) → ② 대상 메시지의 `thread_ts`가 자기 ts와 다르면(스레드 reply) `conversations.replies(ts=thread_ts)`로 root 기준 재조회. 최종 `thread[0]`이 스레드 root이고 그 `user`가 원 질문자입니다.

## 미구현 / Phase 2+

- **Bedrock Knowledge Base (S3 Vectors + RAG)** ingestion pipeline. IAM policy + `sync-notion.yml`/`sync-awsdocs.yml` workflow 스캐폴딩은 있지만, `serverless.yml`이 `S3Bucket`/`KnowledgeBase`/`DataSource`를 provisioning하지 않고 ingestion 스크립트(`scripts/notion/export.py`, `scripts/awsdocs/sync.sh`)도 삭제된 상태. workflow 활성화 시 fail.
- CloudWatch Alarms, X-Ray tracing
- `ko`/`en` 외 언어
