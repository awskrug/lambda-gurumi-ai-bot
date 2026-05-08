# CLAUDE.md

이 파일은 Claude Code(claude.ai/code)가 이 저장소의 코드를 변경할 때 알아야 할 invariant 모음입니다. 일반 사용법은 [README.md](README.md), 깊은 설계 자료는 [docs/](docs/)를 보세요.

## 핵심 파이프라인 — 절대 우회/단축 금지

모든 사용자 메시지는 다음 4단계를 **순서대로** 통과합니다:

```
질문 → 의도·계획 (LLM hop, native function calling) → 툴 사용 (반복) → 응답 (LLM hop)
```

"의도 파악"과 "계획"은 **한 번의 LLM 호출**입니다 — 응답이 두 정보(요청 해석 + 다음 tool_calls)를 동시에 담아 옵니다. `LLMProvider.chat(..., tools=registry.specs())` 한 번. 별도 intent classifier hop을 넣지 마세요.

**불변 규칙**:

1. **의도는 항상 LLM 결정.** 키워드 휴리스틱(예: `"그려"`/`"draw"` → 이미지)으로 우회 금지. LLM이 메시지를 읽고 `tool_calls`로 의도를 표현합니다.
2. **단계 단축 금지.** 명확해 보이는 이미지 요청도 `LLM 계획 → generate_image / edit_image tool_call → tool 실행 → LLM 응답 합성` 전 과정을 거칩니다. 응답 합성을 건너뛰면 caption·후속 대응·tool 에러 처리가 사라집니다.
3. **Tool orchestration은 agent 루프 안에서.** `src/handlers/message.py`는 Slack 관련만(placeholder, streaming, history). `src/agent.py`가 루프를 owns. 의도 탐지를 agent 밖으로 빼지 마세요.
4. **속도 문제는 streaming/infrastructure 문제.** 파이프라인 단축이 아니라 async invocation, 모델 선택, streaming UX로 해결.

## 모듈 layout

`app.py`는 Lambda entrypoint(`serverless.yml: handler: app.lambda_handler`)만. 진짜 로직은 `src/`:

```
app.py                       ← lambda_handler — _worker / receiver / X-Slack-Retry-Num 분기
src/runtime.py               ← 싱글톤 (DDB/SSM/Lambda 클라이언트, Bolt 캐시, bot_user_id, MemoryStore) + accessors + settings + logger
src/router.py                ← receiver path + worker path + per-app Bolt 캐시
src/handlers/message.py      ← _process — app_mention/DM 처리 (allowlist, mention pre-warm, memory load, agent, streaming)
src/handlers/reactions.py    ← _process_reaction + REACTION_HANDLERS dict + 핸들러 (현재 :x: → chat.delete)
src/dedup.py                 ← DedupStore(reserve/is_done/mark_done) + ConversationStore + truncate
src/memory.py                ← MemoryStore — mem:{user_id} 행 (사용자별 영속 메모리, TTL 없음)
src/tools/memory.py          ← remember / forget — system prompt에 자동 주입되는 사용자 메모리 도구
```

`app.py`는 의도적으로 ~70줄. Lambda entrypoint는 deployment contract라서 위치/이름 불변. 실제 로직은 `src/`로 이동해 다른 entrypoint(local CLI, 향후 이벤트 소스)가 Lambda 모듈을 import할 필요 없게.

전체 architecture는 [docs/architecture.md](docs/architecture.md) 참조.

## Cross-module 호출 규약 — load-bearing

`src/router.py`, `src/handlers/message.py`, `src/handlers/reactions.py` 안에서 공유 상태 접근은 **모듈 객체 import + 속성 lookup** 패턴으로만:

```python
# ✅ Late binding — monkeypatch가 동작
from src import runtime
def some_handler(...):
    dedup = runtime._get_dedup()

# ❌ Early binding — monkeypatch 무력화
from src.runtime import _get_dedup
def some_handler(...):
    dedup = _get_dedup()  # 테스트 patch가 안 보임
```

이유: `from X import Y`는 import 시점에 `Y` 객체를 자기 namespace에 binding. 이후 `monkeypatch.setattr(src.runtime, "Y", fake)`가 실행돼도 caller가 들고 있는 reference는 옛 객체. 테스트 420개의 절반 이상이 이 패턴에 의존합니다.

테스트도 같은 규칙 — `_runtime`, `_router`, `_message`, `_reactions` 모듈 객체에 patch.

## 코드 변경 시 깨지기 쉬운 것들

**Agent 루프**:
- `_CompositeProvider` 분기를 `get_llm`에서 제거 → mixed-provider(OpenAI 텍스트 + Bedrock 이미지) 깨짐
- `SlackMentionAgent`의 `finally: self.executor.close()` 제거 → 매 warm 호출마다 ThreadPoolExecutor leak (interpreter 종료까지 unwind 안 됨)
- `ToolExecutor.execute` 예외 처리를 stdlib만 잡도록 좁힘 → provider SDK 예외(`openai.APIError` 등)가 escape해서 agent 루프 abort
- `BedrockProvider.describe_image`의 Nova 분기 제거 → Nova vision이 ValidationException으로 fail (chat()은 family-route하지만 vision도 똑같이 해야 함)

**LLM provider**:
- 새 tool을 `@tool` 데코레이터 없이 등록 → `ToolRegistry.specs()`와 dispatch가 silently desync
- `XAIProvider.edit_image`를 OpenAI SDK `client.images.edit()`로 교체 → xAI 공식 문서가 명시적으로 미지원 (`/v1/images/edits`는 multipart가 아니라 JSON에 `image: {url, type}` 블록). 현재 raw urllib POST는 의도된 우회. 단일 이미지는 객체, 다중은 배열로 보내고 `response_format=b64_json` 강제.
- `BedrockProvider.edit_image`의 `NotImplementedError`를 silent text-to-image fallback으로 바꾸기 → 사용자가 "편집"을 요청했는데 입력 이미지가 무시된 새 이미지가 반환됨. Titan/Nova-Canvas/Stability 각각 image-to-image 스키마가 다르고 검증된 코드 경로가 없으니 명시적 거부 유지.

**Image edit 입력 처리** (`src/tools/image.py`):
- `_collect_input_images`의 explicit `urls` vs `event.files` 우선순위를 섞기로 변경 → 사용자가 fetch_thread_history로 가져온 옛날 이미지를 수정하려는데 현재 첨부 이미지까지 끼어들어옴. `urls`가 주어지면 `event.files`는 무시하는 게 의도.
- SLACK_FILE_HOSTS 가드를 `urls` 입력 경로에서 제거 → 봇 토큰으로 임의 URL fetch 가능 (SSRF). 두 입력 경로 모두 같은 가드를 통과해야 함.
- 봇 토큰을 `ctx.settings.slack_bot_token`에서 읽기로 변경 → Lambda 런타임에서 빈 문자열이라 401. `ctx.slack_client.token` (per-app WebClient 토큰)을 써야 함 — 기존 `read_attached_images` 와 동일 invariant.

**Dedup / 단일 테이블**:
- `DedupStore.reserve`를 read-then-write로 변경 → 동시 worker 두 개가 같은 메시지 처리 가능 (race)
- `id` prefix 스키마(`dedup:` / `done:` / `ctx:` / `app:` / `mem:`) 깨면 다른 종류 행이 충돌
- `app:{api_app_id}` / `mem:{user_id}` 행에 `expire_at` 추가 → DynamoDB TTL이 영구 행을 자동 evict (앱 레지스트리·사용자 메모리 사라짐)
- `dedup:` 단계만 두고 `done:` 마커 제거 → 워커 크래시 시 짧은 TTL이 만료된 *후*에도 retry는 통과해야 하지만 long-lived `dedup:`로 회귀하면 사용자 침묵 발생. 두 단계 분리는 의도된 회복 경로.
- `mark_done` 호출을 dedup TTL보다 늦은 경로(예: history persist 실패 후)로 옮김 → done 미작성으로 retry가 같은 응답을 재생성. mark_done은 응답 전송 *직후*에 호출.
- `DEFAULT_RESERVE_TTL`을 Lambda timeout(`serverless.yml: timeout: 300`)보다 짧게 설정 → 무거운 도구(`generate_image`/`edit_image`, 240s) 실행 도중 dedup row 만료 → 동일 payload 재전달 시 `reserve` 통과로 중복 처리. TTL은 *Lambda timeout 이상*으로 유지.

**App metadata**:
- `AppMetadataStore.record(...)`에서 `ReturnValues=ALL_NEW` 제거 → per-app override resolution이 별도 GetItem이 되거나(latency+cost) silently regress하여 항상 글로벌
- `record(...)` raise 시 fail-closed로 변경 → DynamoDB outage가 봇 전체를 막음 (현재는 글로벌 env var fallback)
- Metadata recording을 dedup 통과 전으로 옮김 → Slack/Lambda retry가 last_seen_at을 inflate
- Lambda IAM에서 `dynamodb:UpdateItem` 제거 → `record()`가 silent fail, `app:` 행이 안 생기고 per-app override가 글로벌로 silent regression

**멀티테넌트 credential resolution**:
- Lambda invoke payload에 bot_token/signing_secret 포함 → CloudTrail 등에 시크릿 노출. `_enqueue_worker`는 `api_app_id`만 운반.
- `_bolt_apps` 캐시 키를 `api_app_id`만으로 사용(secret 튜플 빠뜨림) → Parameter Store rotation이 컨테이너 죽기 전까지 반영 안 됨
- `url_verification`에 시그니처 검증 추가 → 글로벌 secret 필요(멀티테넌트 깨짐) 또는 known secrets 전수 시도(앱 존재 leak). Body가 actionable payload 없으니 challenge echo가 정답.
- `CredentialsStore`의 negative-result 캐시 제거 → 미설정 앱의 트래픽 burst가 SSM `GetParameters` quota를 burn

**Per-app override 3-state 계약**:
- "속성 absent" vs "속성 = `[]` / `\"\"`"를 같은 의미로 collapse → override 의도 손실. `absent → 글로벌 fallback`, `present → 글로벌 무시`, `[]/"" → 명시적 빈값 오버라이드` 세 상태를 모두 distinct하게 유지.
- `SYSTEM_MESSAGE`에 per-app override 추가 → 한 앱이 보안 정책을 약화시킬 수 있음. `SYSTEM_MESSAGE`는 운영자 정책이라 글로벌 전용. `PERSONA_MESSAGE`(스타일/톤)만 per-app.

**User memory (`mem:{user_id}`)**:
- `mem:{user_id}` 행에 `expire_at` 추가 → 영구 사용자 메모리가 TTL evict로 사라짐 (`app:` 행과 동일 invariant).
- 메모리 row id를 `mem:{api_app_id}:{user_id}`로 분리하지 않음 — 운영 가정에서 user_id가 앱 간 unique. 가정이 깨지면 row id를 그렇게 바꾸고 store API는 그대로 둘 수 있음.
- `remember`/`forget` 호출이 같은 turn의 system prompt에 즉시 반영되도록 변경 → LLM이 자기가 방금 저장한 값을 보고 자기-확인 루프에 빠짐. 메모리는 agent 생성 시점에 한 번만 로드, 다음 turn부터 반영.
- Lambda IAM에서 `dynamodb:DeleteItem` 제거 → `MemoryStore.delete`가 마지막 entry 삭제 시 `delete_item`을 호출해 AccessDenied. 사용자가 메모리를 모두 잊으려 할 때만 발생하는 silent regression이라 일반 통합 테스트로 안 잡힘.

**Mention 처리** (`src/handlers/message.py`):
- `MENTION_RE`로 모든 `<@U…>` 멘션을 통째 strip → LLM이 함께 멘션된 다른 사용자의 user_id를 못 봄. `fetch_user_profile`이 cache 빈 상태에서 평문 display name을 받아 ValueError로 fail (CloudWatch에서 관찰된 incident). 봇 자신의 mention만 `_strip_bot_mention(text, bot_user_id)`로 제거. 다른 user mention은 보존.
- 메시지 mention pre-warm을 제거 → cache cold 상태에서 LLM이 fetch_thread_history 누락 시 회귀. `_USER_MENTION_RE`로 추출 후 `user_name_cache.warm` 호출 유지.
- `fetch_user_profile`의 cache-miss 자동 fallback(`_warm_cache_from_thread`) 제거 → A/B 둘 다 누락된 케이스에서 사용자 침묵.

**Reaction 처리**:
- `_get_bot_user_id` 권한 체크 우회 → 다른 봇/사람 메시지에 `chat.delete` 시도 → 403. `auth.test` 결과는 success만 캐시(failure는 매번 retry)
- 메시지 흐름과 reaction 흐름의 `ALLOWED_USER_IDS=[]` 의미를 통일 → 의도된 비대칭 깨짐. 메시지=open by default(`[]`=모두 허용), reaction-delete=closed by default(`[]`=원 질문자만)
- Reaction handler가 공통 dispatcher 우회 → dedup/request_id/item.type 체크 skip. 새 reaction은 항상 `REACTION_HANDLERS`에 등록하고 `_process_reaction`을 통하도록.

**Channel allowlist + DM 비대칭**:
- DM에 channel allowlist 적용 → DM 채널 ID(`D...`)는 보통 `ALLOWED_CHANNEL_IDS`에 enroll 안 되니, allowlist 설정 순간 모든 DM 차단. `_process()`는 `is_dm=True`일 때 channel 체크 건너뜀.

**Slack 응답 처리**:
- `LoggerAdapter.info(extra=…)`로 변경 → Python 3.12에서 `LoggerAdapter.process()`가 `extra`를 덮어씀. `log_event`는 `logger.logger`(underlying `Logger`)로 dispatch 유지.
- `MessageFormatter` 분할 우선순위(코드펜스 → 문단 → 문장 → hard slice) 변경 → 코드블록이 잘리거나 문맥 없는 chunk

**SSRF 가드**:
- `_validate_public_https_url` 제거 (`fetch_webpage`) → RFC1918 / cloud metadata(`169.254.169.254`) fetch 가능
- `fetch_webpage` raw fallback의 `_NoRedirectHandler` 제거 → 302가 사설 호스트로 우회
- Slack file fetch의 `_http_get` helper(`_NoRedirectHandler` 적용)를 직접 `urlopen`으로 교체 → 3xx redirect가 봇 Authorization 헤더를 cross-host로 leak. `src/tools/slack.py`/`src/tools/image.py`의 모든 Slack 다운로드는 `_http_get`을 거쳐야 함.
- Slack file fetch host allowlist(`SLACK_FILE_HOSTS`) 제거 → 봇 토큰으로 임의 URL fetch
- `attach_image_from_url`의 magic bytes 검증(`_detect_image_mime`) 제거 → 악성 서버가 `Content-Type: image/png`로 HTML/SVG를 줘도 Slack에 업로드. PNG/JPEG/GIF/WEBP/BMP 시그니처 8바이트 검증 유지.
- DNS rebinding 한계 인지: `_validate_public_https_url`의 pre-flight `getaddrinfo`와 실제 TCP connect는 별개 lookup. Lambda는 VPC 밖이라 영향 제한적이지만 VPC/private-subnet egress 추가 시 재검토 필요.

**Receiver/worker fallback**:
- `_enqueue_worker`의 invoke 실패 분기에서 `_process_worker`를 inline 실행 → receiver는 API Gateway 30s + Slack ack 3s 윈도우만 있고 inline agent run은 둘 다 초과 → retry 폭주. inline은 `AWS_LAMBDA_FUNCTION_NAME` 미설정인 로컬/테스트 경로에서만. Lambda 환경 invoke 실패는 best-effort `chat_postMessage` 안내 후 drop.

운영 정책에 가까운 것들(예: `scripts/apps.py delete`의 `app_id` 재입력 확인 약화)은 [docs/operations.md](docs/operations.md)에 정리되어 있습니다.

## Testing — 패치 경로 invariant

테스트 파일과 책임:

| 파일 | 대상 |
|------|------|
| `tests/test_app.py` | `app.lambda_handler` (worker/receiver/retry 분기) |
| `tests/test_router.py` | `src.router` (receiver path, worker path, Bolt 캐시) |
| `tests/test_handlers_message.py` | `src.handlers.message._process` |
| `tests/test_handlers_reactions.py` | `src.handlers.reactions` (dispatcher + `:x:` 핸들러) |
| `tests/llms/`, `tests/tools/` | provider/tool 단위 테스트 |
| `tests/_helpers.py` | 공유 픽스처 (`_FakeCreds`, `_FakeDedup`, `_NullMetadata`) |
| `tests/tools/_helpers.py` | tool 테스트 공유 픽스처 (`_ctx`, `_settings`, `_streamed_read`) |

**패치 경로 규약**: 테스트는 `from src import router as _router; from src import runtime as _runtime; from src.handlers import message as _message; from src.handlers import reactions as _reactions` 식으로 모듈 객체를 import하고, 그 객체에 patch (`monkeypatch.setattr(_runtime, "_get_dedup", ...)`).

`monkeypatch.setattr(app_module, "...")` 식으로 다른 모듈에 있는 상태를 patch하면 *동작 안 합니다* — Cross-module 호출 규약 참조.

**Network 패치도 같은 규칙**: submodule(import한 곳)에 patch — `patch("src.tools.web.urllib.request.urlopen")` 같이 (package 아님).

## Excluded (Phase 2+)

- **Bedrock Knowledge Base (S3 Vectors + RAG)** ingestion pipeline. IAM policy + `sync-notion.yml`/`sync-awsdocs.yml` workflow 스캐폴딩만 있고 `serverless.yml`이 `S3Bucket`/`KnowledgeBase`/`DataSource`를 provisioning하지 않음. ingestion 스크립트도 삭제됨. Workflow 활성화 시 fail.
- CloudWatch Alarms, X-Ray tracing
- `ko`/`en` 외 언어
