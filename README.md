# lambda-gurumi-bot

Slack 멘션·DM 을 AWS Lambda 에서 처리하고, OpenAI · AWS Bedrock · xAI(Grok) LLM 으로 네이티브 **function calling** 기반 툴 오케스트레이션을 수행하는 봇입니다.

![Gurumi Bot](images/gurumi-bot.png)

## 봇의 처리 흐름 (절대 생략하지 않는다)

모든 사용자 메시지는 다음 네 단계를 **순서대로** 통과합니다:

```
질문 ── 의도·계획 ── 툴 사용 (반복) ── 응답
 (user)    (LLM)        (tools)        (LLM)
```

**의도 파악과 계획은 한 번의 LLM 호출로 통합**되어 있습니다 (OpenAI / Claude / Nova 의 native function calling). 같은 응답에 "무슨 요청인지 파악한 결과" 와 "다음에 부를 tool_calls" 가 함께 담겨 옵니다. 별도의 intent 분류 hop 을 추가하지 않습니다.

- **의도·계획은 LLM 이 한다.** 키워드 매칭(예: `"그려"` → 이미지)으로 우회하지 않는다. LLM 이 메시지를 읽고 `tool_calls` 로 의도를 표현한다.
- **단계 단축 금지.** 이미지 요청처럼 명확해 보여도 `LLM 판단 → generate_image tool → LLM 응답 합성` 전 과정을 거친다. 응답 합성 단계를 건너뛰면 caption·후속 대응·에러 처리가 사라진다.
- **Agent 루프는 `src/agent.py` 안에** 있고, `app.py` 는 Slack 관련 부분(placeholder, streaming, 히스토리) 만 담당한다.
- **속도 문제는 파이프라인 단축이 아닌** 스트리밍·비동기·모델 선택으로 해결한다.

## 주요 기능

- **이벤트**: `app_mention`, DM(`message.im`)
- **Provider**: OpenAI · AWS Bedrock(Anthropic Claude 3/3.5/4.x · Amazon Nova) · xAI(Grok) 선택 가능
- **Tools (네이티브 function calling)**
  - `read_attached_images` — 첨부 이미지 Vision 요약
  - `read_attached_document` — 첨부 PDF/텍스트 파일 추출 (페이지·바이트·문자 상한 적용)
  - `fetch_thread_history` — 스레드 히스토리 조회
  - `search_web` — Tavily (TAVILY_API_KEY 설정 시) 또는 DuckDuckGo
  - `fetch_webpage` — 공개 HTTPS 웹 페이지 본문·링크 추출 (Jina Reader 우선 + raw fallback, SSRF 가드)
  - `generate_image` — 이미지 생성 후 Slack 업로드
  - `get_current_time` — 서버 기본 TZ(또는 `timezone` 인자) 로 현재 시각/요일 반환
- **Production 기반**
  - DynamoDB 조건부 put 으로 Slack 재시도 **중복 제거**
  - 채널 allowlist · 유저당 동시 요청 **throttle**
  - DynamoDB 기반 **스레드 대화 메모리** (TTL 1h)
  - 긴 응답 **계층적 분할** 전송 (코드블록 → 문단 → 문장 → hard slice) + `MAX_LEN_SLACK` 기반 rolling 스트리밍
  - `chat_postMessage` + `chat_update` 반복으로 스트리밍 (네이티브 `chat.startStream` 계열은 `enable_native=True` 옵션). 툴 실행 구간에는 `assistant_threads_setStatus` 타이핑 인디케이터만 표시, **첫 content delta 도착 시점에** placeholder 메시지를 지연 posting — 상태 UI 와 placeholder 중복 표시 방지
  - 구조화 JSON 로깅 + request_id, agent 루프 관찰값 기록
  - 에러 메시지 sanitize (토큰·경로 redaction)

## 환경 변수

> **Slack 시크릿은 환경변수가 아니라 SSM Parameter Store에 둔다.** 봇은
> 멀티 테넌트로 동작하며 요청마다 `api_app_id` 로 시크릿을 조회합니다.
> 새 Slack 앱을 등록할 때마다 두 개의 SecureString 파라미터를 미리 만들어
> 둬야 합니다 (앱이 이벤트를 보내기 전에). 운영 CLI(`scripts/apps.py`)가
> 권장 경로 — 시크릿 마스킹 입력, 값 출력 없음, `delete` 시 `app_id`
> 재입력 확인:
>
> ```bash
> python scripts/apps.py list                       # 등록된 앱 + SSM/DDB 상태
> python scripts/apps.py get A0123ABC               # 단일 앱 상세
> python scripts/apps.py set A0123ABC               # getpass 인터랙티브
> SIG=… TOK=… python scripts/apps.py set A0123ABC \
>   --signing-secret-env=SIG --bot-token-env=TOK    # 스크립트용
> python scripts/apps.py delete A0123ABC            # app_id 재입력 후 양쪽 삭제
> ```
>
> 또는 직접 awscli (확인 절차·마스킹 없음):
>
> ```bash
> aws ssm put-parameter --type SecureString \
>   --name /gurumi-bot/apps/A0123ABC/signing_secret --value "$SIGNING"
> aws ssm put-parameter --type SecureString \
>   --name /gurumi-bot/apps/A0123ABC/bot_token --value "$BOT_TOKEN"
> ```
>
> 미설정 상태로 이벤트가 들어오면 구조화 로그에 `request.unknown_app` 으로
> 기록되고 HTTP 200 으로 응답해 Slack 재시도 폭주를 방지합니다.
> `SLACK_BOT_TOKEN` env var 는 `localtest.py` 가 Slack 도구를 실제로 호출할
> 때만 쓰이는 *로컬 전용 편의 변수* 입니다.

| 변수 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `SSM_PARAMS_PREFIX` | | `/gurumi-bot/apps` | 멀티테넌트 시크릿 SSM 경로 prefix. IAM resource ARN(`serverless.yml`)과 매치 필요 |
| `SSM_CACHE_TTL_SECONDS` | | `300` | 시크릿 in-process 캐시 TTL (≥10). 로테이션은 이 시간 내 반영 |
| `SLACK_BOT_TOKEN` | (localtest only) | — | `localtest.py` 에서만 사용. Lambda 런타임은 SSM 만 본다 |
| `OPENAI_API_KEY` | OpenAI 사용 시 | — | OpenAI API 키 |
| `XAI_API_KEY` | xAI 사용 시 | — | xAI (Grok) API 키 — https://console.x.ai |
| `TAVILY_API_KEY` | | — | 설정 시 Tavily 웹 검색 활성화 |
| `LLM_PROVIDER` | | `openai` | `openai` / `bedrock` / `xai` |
| `LLM_MODEL` | | `gpt-4o-mini` | 텍스트 모델 |
| `IMAGE_PROVIDER` | | `openai` | `openai` / `bedrock` / `xai` |
| `IMAGE_MODEL` | | `gpt-image-1` | 이미지 모델 |
| `AGENT_MAX_STEPS` | | `3` | tool 루프 최대 iteration |
| `RESPONSE_LANGUAGE` | | `ko` | `ko` / `en` |
| `DYNAMODB_TABLE_NAME` | | `lambda-gurumi-bot-dev` | dedup / 대화 저장 테이블 |
| `AWS_REGION` | | `us-east-1` | AWS 리전 |
| `ALLOWED_CHANNEL_IDS` | | (empty) | **앱별 오버라이드 가능** (DynamoDB → `scripts/apps.py acl set`). 글로벌 fallback. 비어있으면 모든 채널 허용. **DM 은 채널 allowlist 대상이 아님** — allowlist 를 설정해도 DM 경로는 항상 허용 |
| `ALLOWED_CHANNEL_MESSAGE` | | — | 비허용 채널 응답 메시지 (DM 에는 적용되지 않음). `{}` 가 있으면 *effective* allowlist 의 첫 채널을 `<#ID>` 멘션 형태로 치환 |
| `ALLOWED_USER_IDS` | | (empty) | **앱별 오버라이드 가능** (DynamoDB → `scripts/apps.py acl set`). 글로벌 fallback. 비어있으면 모든 유저 허용. **채널·DM 모든 경로에 적용** — DM 도 차단 |
| `ALLOWED_USER_MESSAGE` | | — | 비허용 유저 응답 메시지. `{}` 가 있으면 *effective* allowlist 의 첫 유저를 `<@ID>` 멘션 형태로 치환 |
| `MAX_LEN_SLACK` | | `3000` | 메시지 분할 기준 (≥500). `.env.example` · `serverless.yml` 기본 `3000`, 미지정 시 `config.py` 폴백 `2000`. |
| `MAX_OUTPUT_TOKENS` | | `4096` | LLM hop 당 출력 토큰 상한 (≥256) |
| `MAX_THROTTLE_COUNT` | | `100` | 유저별 동시 요청 상한 |
| `MAX_HISTORY_CHARS` | | `4000` | 저장되는 대화 직렬화 최대 길이 |
| `DEFAULT_TIMEZONE` | | `Asia/Seoul` | `get_current_time` 기본 TZ (IANA). 잘못된 이름이면 기본값으로 폴백 + 경고 |
| `MAX_DOC_CHARS` | | `20000` | `read_attached_document` 추출 텍스트 최대 문자수 (≥1000) |
| `MAX_DOC_PAGES` | | `50` | `read_attached_document` PDF 최대 페이지수 (≥1) |
| `MAX_DOC_BYTES` | | `26214400` | `read_attached_document` 다운로드 최대 바이트 (기본 25MB, ≥65536) |
| `MAX_WEB_CHARS` | | `8000` | `fetch_webpage` 반환 본문 최대 문자수 (≥500) |
| `MAX_WEB_BYTES` | | `2097152` | `fetch_webpage` 다운로드 최대 바이트 (기본 2MB, ≥65536) |
| `MAX_WEB_LINKS` | | `20` | `fetch_webpage` 반환 링크 최대 개수 (≥0) |
| `JINA_READER_BASE` | | `https://r.jina.ai` | `fetch_webpage` 가 호출하는 Jina Reader 베이스 URL. `https://` 가 아니면 기본값으로 폴백 |
| `BOT_CURSOR` | | `:robot_face:` | 플레이스홀더·스트림 인디케이터 이모지 |
| `SYSTEM_MESSAGE` | | — | 작업 규칙에 append 되는 추가 운영 정책 (예: 조직·채널 제약). base 를 덮어쓰지 않음 |
| `PERSONA_MESSAGE` | | — | 답변 스타일/톤 (예: `"자연스러운 한국어로 핵심부터 답한다"`) |
| `LOG_LEVEL` | | `INFO` | 로그 레벨 |

### 앱별 ACL 오버라이드 (DynamoDB)

`ALLOWED_CHANNEL_IDS` / `ALLOWED_USER_IDS` 는 **배포 단위 기본값**입니다. 각
Slack 앱(`api_app_id`)은 DynamoDB `app:{app_id}` 행에 동일 이름의 속성을
추가해 글로벌을 *덮어쓸* 수 있습니다. 세 가지 상태:

| DynamoDB 속성 상태 | 동작 |
|--------------------|------|
| 속성 *없음* | 글로벌 env var 사용 (기본 동작) |
| 속성 = `[C1, C2]` | per-app 값 사용, 글로벌 무시 |
| 속성 = `[]` | "이 앱은 명시적으로 모두 허용" — 제한적인 글로벌도 무시 |

운영은 CLI로:

```bash
python scripts/apps.py acl get A0123ABC                    # 현재 상태
python scripts/apps.py acl set A0123ABC --channels=C1,C2   # per-app 채널 제한
python scripts/apps.py acl set A0123ABC --channels=""      # 명시적 허용 (글로벌 무시)
python scripts/apps.py acl set A0123ABC --users=U1         # per-app 유저 제한
python scripts/apps.py acl unset A0123ABC --channels --users  # 글로벌로 복귀
```

차단 메시지(`ALLOWED_CHANNEL_MESSAGE` 등)의 `{}` 치환은 *effective* 리스트의
첫 항목을 사용 — per-app 오버라이드가 적용된 앱은 자기 채널/유저로 안내됩니다.
메시지 템플릿 자체는 글로벌로 유지.

## 모델 매트릭스

| 용도 | OpenAI | Bedrock | xAI (Grok) |
|------|--------|---------|------------|
| 텍스트 + tool calling | `gpt-4o-mini`, `gpt-4o`, `gpt-5-*`, `o1/o3/o4` | `us.anthropic.claude-opus-4-6-v1`, `us.anthropic.claude-sonnet-4-5-...`, `amazon.nova-pro-v1:0` | `grok-4-1-fast-reasoning`, `grok-4.20-0309-reasoning`, `grok-4.20-multi-agent-0309` |
| 이미지 생성 | `gpt-image-1`, `dall-e-3` | `amazon.nova-canvas-v1:0`, `amazon.titan-image-generator-v2:0` | `grok-imagine-image`, `grok-imagine-image-pro` |

- Claude 는 Messages API (`tools=[{name, description, input_schema}]`), Nova 는 Converse API (`toolConfig`) 로 자동 분기됩니다.
- xAI 는 OpenAI wire 호환이라 OpenAI Python SDK 에 `base_url="https://api.x.ai/v1"` 만 swap 해서 호출합니다. 별도 `XAIProvider` 클래스로 분리되어 있습니다.
- Bedrock 최신 모델은 `us./eu./apac./global.` inference-profile prefix 가 붙은 ID 로만 호출됩니다. `BedrockProvider` 가 자동 인식합니다.

## 로컬 개발

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt

cp .env.example .env.local      # 값 채우기

# CLI 실행 (스트리밍이 기본값)
python localtest.py "오늘 서울 날씨"
python localtest.py --no-stream "React 훅 설명해줘"   # 전체 답변을 한 번에 출력
python localtest.py --quiet-steps "…"                # 중간 step 로그 숨김
python localtest.py                                  # 대화형 (stdin, Ctrl+D)

# 테스트 (189 테스트, 커버리지 89% — `pytest.ini` 기준)
python -m pytest --cov=src --cov-report=term-missing
python -m pytest tests/llms/test_bedrock.py -v                      # 패키지 단위
python -m pytest tests/tools/test_web.py::test_fetch_webpage_jina_happy_path -v   # 단일 케이스
```

`.env.local` 은 `src/config.py` 가 python-dotenv 로 자동 로드합니다. `SLACK_BOT_TOKEN` 이 placeholder 이면 `localtest.py` 가 Slack 호출을 stub 으로 대체하고 `generate_image` 결과물은 `./.uploads/` 에 파일로 저장됩니다.

## 배포 (Serverless Framework v3)

### 1. IAM OIDC role 준비 (한 번만)

`role/lambda-gurumi-bot` 을 AWS 계정에 생성하고 GitHub OIDC trust + 배포용 policy 를 연결합니다. 템플릿과 상세 절차는 `.github/aws-role/` 에 있습니다:

```bash
cd .github/aws-role
export NAME="lambda-gurumi-bot"
aws iam create-role --role-name "${NAME}" --assume-role-policy-document file://trust-policy.json
aws iam create-policy --policy-name "${NAME}" --policy-document file://role-policy.json
export ACCOUNT_ID=$(aws sts get-caller-identity | jq -r .Account)
aws iam attach-role-policy --role-name "${NAME}" --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${NAME}"
```

### 2. GitHub 저장소 설정

- **Secrets**: `AWS_ACCOUNT_ID`, `OPENAI_API_KEY`, `XAI_API_KEY`(xAI 사용 시), `TAVILY_API_KEY`(선택). Slack 시크릿은 SSM Parameter Store 에 별도 등록 — CI 시크릿 아님.
- **Variables**: `LLM_PROVIDER`, `LLM_MODEL`, `IMAGE_PROVIDER`, `IMAGE_MODEL`, `RESPONSE_LANGUAGE`, `ALLOWED_CHANNEL_IDS`, `ALLOWED_CHANNEL_MESSAGE`, `ALLOWED_USER_IDS`, `ALLOWED_USER_MESSAGE`, `SYSTEM_MESSAGE`, `PERSONA_MESSAGE`, `BOT_CURSOR`, `MAX_LEN_SLACK`, `MAX_OUTPUT_TOKENS`, `MAX_THROTTLE_COUNT`, `MAX_HISTORY_CHARS`, `AGENT_MAX_STEPS`, `LOG_LEVEL`, `DEFAULT_TIMEZONE`, `MAX_DOC_CHARS`, `MAX_DOC_PAGES`, `MAX_DOC_BYTES`, `MAX_WEB_CHARS`, `MAX_WEB_BYTES`, `MAX_WEB_LINKS`, `JINA_READER_BASE`, `SSM_PARAMS_PREFIX`, `SSM_CACHE_TTL_SECONDS`

### 3. 배포

`main` 브랜치에 push 하면 `.github/workflows/push.yml` 이 pytest (`--cov=src`) → Serverless v3 deploy 순으로 수행합니다. 수동 실행은 `workflow_dispatch`.

```bash
# 로컬 배포 (선택)
npm i -g serverless@3 && npm i serverless-python-requirements
# Secrets + Variables 를 현재 셸에 export 한 뒤
serverless deploy --stage dev --region us-east-1
```

DynamoDB 테이블 (해시키 `id`, GSI `user-index`, TTL `expire_at`) 은 CloudFormation 이 생성합니다.

### 4. 추가 워크플로

| 파일 | 역할 | 상태 |
|------|------|------|
| `push.yml` | 테스트 + Lambda 배포 | 활성 |
| `sync-notion.yml` | Notion → S3 → Bedrock KB ingestion | `workflow_dispatch` 전용, `vars.ENABLE_SYNC_NOTION == 'true'` gating. **Phase 2 미완성** (아래 참조) |
| `sync-awsdocs.yml` | AWS 공식 문서 → S3 → KB ingestion | 위와 동일 패턴, `ENABLE_SYNC_AWSDOCS` gating |

## 코드 구조

```
app.py                    Lambda 엔트리 · Slack Bolt 핸들러 · `_process()` 흐름
src/
├── agent.py              Agent 루프 (native function calling 반복)
├── config.py             Settings (env → dataclass, lazy validation)
├── dedup.py              DynamoDB 기반 중복 제거 / 대화 메모리
├── logging_utils.py      구조화 JSON 로깅 + request_id
├── slack_helpers.py      메시지 분할·스트리밍·사용자 캐시
├── llms/                 LLM provider 패키지
│   ├── base.py              Protocol + 공통 타입 + _with_retry
│   ├── openai_wire.py       OpenAI wire 공통 (OpenAI·xAI 공유)
│   ├── openai.py            OpenAIProvider
│   ├── xai.py               XAIProvider
│   ├── bedrock.py           BedrockProvider (Anthropic·Nova·Stability)
│   ├── composite.py         _CompositeProvider (text+image 분리 설정)
│   └── factory.py           get_llm
└── tools/                Tool 패키지
    ├── registry.py          ToolDef · ToolRegistry · @tool · ToolExecutor
    ├── slack.py             read_attached_images · read_attached_document · fetch_thread_history
    ├── search.py            search_web (DuckDuckGo / Tavily)
    ├── web.py               fetch_webpage + SSRF 가드 + HTML/Jina 파서
    ├── image.py             generate_image
    └── time.py              get_current_time
```

테스트는 소스 구조를 그대로 미러링한 `tests/llms/`, `tests/tools/` 에 있습니다.

## 확장하기

새로운 tool 이나 LLM provider 는 파일 하나를 추가하는 것으로 끝납니다. 자세한 단계는 [`docs/extending.md`](docs/extending.md) 를 참고하세요.

짧게 말해:

- **새 tool**: `src/tools/<name>.py` 에 `@tool(default_registry, ...)` 로 데코레이트된 함수를 정의하고, `src/tools/__init__.py` 의 side-effect import 블록에 이름을 추가하면 `default_registry` 가 자동으로 등록합니다.
- **새 LLM provider**: `src/llms/<name>.py` 에 `LLMProvider` Protocol 을 만족하는 클래스를 작성하고 `src/llms/factory.py` 의 `get_llm` 분기에 연결합니다.

## 아키텍처

```
┌────────────────┐  POST /slack/events
│ Slack workspace│──────────────────┐
└────────────────┘                  ▼
                    ┌───────────────────────────────────┐
                    │ API Gateway → Lambda (app.py)     │
                    │ ├─ X-Slack-Retry-Num early return │
                    │ └─ SlackRequestHandler (Bolt)     │
                    └────────┬───────────────────┬──────┘
                             │                   │
                  ┌──────────▼─────────┐  ┌──────▼─────────┐
                  │ app_mention handler│  │ message handler│
                  └──────────┬─────────┘  └──────┬─────────┘
                             └──────┬────────────┘
                                    ▼
                ┌───────────────────────────────────────────┐
                │ _process()                                │
                │  1. DedupStore.reserve (conditional put)  │
                │  2. channel_allowed / throttle            │
                │  3. set_thread_status + placeholder say   │
                │  4. ConversationStore.get → history       │
                │  5. SlackMentionAgent.run ──┐             │
                │  6. send_long_message       │             │
                │  7. ConversationStore.put   │             │
                └─────────────────────────────┼─────────────┘
                                              │
                      ┌───────────────────────▼───────────────┐
                      │ Agent loop (native function calling)  │
                      │  LLM.chat(messages, tools=registry)   │
                      │   ↓ tool_calls?                       │
                      │  ToolExecutor.execute (per-call t/o)  │
                      │   ↓ role=tool result                  │
                      │  (loop up to AGENT_MAX_STEPS)         │
                      │  streaming chat_update on final step  │
                      └────────────┬──────────────────────────┘
                                   │
                   ┌───────────────┼────────────────┐
                   ▼               ▼                ▼
            ┌───────────┐   ┌────────────┐  ┌──────────────┐
            │ OpenAI    │   │ Bedrock    │  │ Slack Web API│
            │ Chat API  │   │ Messages / │  │ (tools)      │
            │ Vision    │   │ Converse   │  └──────────────┘
            └───────────┘   └────────────┘
                                   ▲
                                   │
                            ┌──────┴─────┐
                            │ DynamoDB   │
                            │ (dedup+ctx)│
                            └────────────┘
```
