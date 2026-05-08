# lambda-gurumi-bot

Slack 멘션·DM 을 AWS Lambda 에서 처리하고, OpenAI · AWS Bedrock · xAI(Grok) LLM 으로 네이티브 **function calling** 기반 툴 오케스트레이션을 수행하는 봇입니다.

![Gurumi Bot](images/gurumi-bot.png)

## 처리 흐름

모든 사용자 메시지는 다음 4단계를 **순서대로** 통과합니다 (단축 금지 — 자세한 근거는 [CLAUDE.md](CLAUDE.md)):

```
질문 → 의도·계획 → 툴 사용 (반복) → 응답
(user)    (LLM)       (tools)         (LLM)
```

의도 파악과 계획은 **한 번의 LLM 호출**로 통합 (OpenAI/Claude/Nova의 native function calling). 같은 응답에 요청 해석 + 다음 `tool_calls`가 함께 담겨 옵니다.

## 주요 기능

- **이벤트**: `app_mention`, DM(`message.im`), `reaction_added`(`:x:`로 봇 글 삭제 — [docs/reactions.md](docs/reactions.md))
- **Provider**: OpenAI · AWS Bedrock(Anthropic Claude 3/3.5/4.x · Amazon Nova) · xAI(Grok) 선택 가능
- **Tools (네이티브 function calling)**
  - `read_attached_images` — 첨부 이미지 Vision 요약
  - `read_attached_document` — 첨부 PDF/텍스트 추출 (페이지·바이트·문자 상한)
  - `fetch_thread_history` — 스레드 히스토리 조회
  - `fetch_user_profile` — Slack 유저 프로필(display_name, real_name, image_url) 조회. 캐시 미스 시 thread를 1회 자동 warm 후 재시도
  - `search_web` — Tavily (`TAVILY_API_KEY` 설정 시) 또는 DuckDuckGo
  - `search_images` — Tavily 이미지 검색(`TAVILY_API_KEY` 필수, DDG fallback 없음)
  - `fetch_webpage` — 공개 HTTPS 웹페이지 본문·링크 (Jina Reader 우선 + raw fallback, SSRF 가드)
  - `generate_image` — 텍스트 프롬프트로 이미지 생성 후 Slack 업로드
  - `edit_image` — 첨부된 이미지(또는 스레드 이전 이미지 URL)를 프롬프트로 편집 후 Slack 업로드. OpenAI/xAI 지원, Bedrock 미지원
  - `attach_image_from_url` — 외부 공개 HTTPS 이미지를 다운로드해 Slack 스레드에 첨부 (SSRF 가드 + magic bytes 검증)
  - `get_current_time` — 서버 기본 TZ 또는 인자로 현재 시각/요일
  - `remember` / `forget` — 사용자별 영속 메모리. 다음 turn부터 system prompt에 자동 주입(별도 `recall` 도구 없음)
- **Production 기반**
  - **멀티테넌트**: 단일 배포로 여러 Slack 앱 서빙. 시크릿은 SSM Parameter Store에서 `api_app_id` 키로 per-request resolve
  - **앱별 오버라이드**: ACL(channel/user) + persona를 앱별로 글로벌 env var와 다르게 설정 가능
  - **Receiver/worker 분리**: API Gateway 30초 제한과 무관하게 worker는 Lambda 300초 budget 사용 (async self-invoke)
  - DynamoDB 조건부 put으로 Slack/Lambda 재시도 **중복 제거**
  - 채널 allowlist + 유저당 동시 요청 **throttle**
  - DynamoDB 기반 **스레드 대화 메모리** (TTL 1h)
  - 긴 응답 **계층적 분할** (코드블록 → 문단 → 문장 → hard slice) + `MAX_LEN_SLACK` 기반 rolling 스트리밍
  - 첫 content delta 도착 시점에 placeholder 메시지 지연 posting — status UI와 중복 방지
  - 구조화 JSON 로깅 + request_id, 에러 메시지 sanitize

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
| `SYSTEM_MESSAGE` | | — | 작업 규칙에 append 되는 추가 운영 정책. base 를 덮어쓰지 않음. **글로벌 전용** (per-app 오버라이드 없음 — 보안·정책 일관성) |
| `PERSONA_MESSAGE` | | — | 답변 스타일/톤 (예: `"자연스러운 한국어로 핵심부터 답한다"`). **앱별 오버라이드 가능** (DynamoDB → `scripts/apps.py persona set`) |
| `LOG_LEVEL` | | `INFO` | 로그 레벨 |

### 앱별 오버라이드 (DynamoDB)

`ALLOWED_CHANNEL_IDS` / `ALLOWED_USER_IDS` / `PERSONA_MESSAGE` 는 **배포 단위
기본값**입니다. 각 Slack 앱(`api_app_id`)은 DynamoDB `app:{app_id}` 행에
같은 이름의 속성을 추가해 글로벌을 *덮어쓸* 수 있습니다. 세 가지 상태:

| 속성 상태 | 동작 |
|--------------------|------|
| 속성 *없음* | 글로벌 env var 사용 (기본 동작) |
| 속성 = `[C1, C2]` / `"text"` | per-app 값 사용, 글로벌 무시 |
| 속성 = `[]` / `""` | 빈값을 명시적 오버라이드로 보존 — 리스트는 "이 앱은 모두 허용", 문자열은 "이 앱은 페르소나 없음" 의미 |

운영은 CLI로:

```bash
# ACL (channel/user allowlist)
python scripts/apps.py acl get A0123ABC
python scripts/apps.py acl set A0123ABC --channels=C1,C2   # per-app 채널 제한
python scripts/apps.py acl set A0123ABC --channels=""      # 명시적 허용 (글로벌 무시)
python scripts/apps.py acl unset A0123ABC --channels --users

# 페르소나 (PERSONA_MESSAGE)
python scripts/apps.py persona get A0123ABC
python scripts/apps.py persona set A0123ABC "당신은 친근한 어시스턴트"
python scripts/apps.py persona set A0123ABC --from-file persona.txt    # 멀티라인
python scripts/apps.py persona set A0123ABC ""                         # 명시적 페르소나 없음
python scripts/apps.py persona unset A0123ABC

# 앱 식별 (apps list NAME 컬럼)
python scripts/apps.py refresh A0123ABC                                # auth.test 로 team/bot 갱신
python scripts/apps.py name set A0123ABC "Production - Acme"           # 운영자 라벨 (최우선)
python scripts/apps.py name unset A0123ABC                             # 자동 채워진 team/bot 으로 복귀
# `apps set` 은 기본으로 auth.test 호출해서 즉시 team_name/bot_user_name 채움. --no-verify 로 스킵.
```

차단 메시지(`ALLOWED_CHANNEL_MESSAGE` 등)의 `{}` 치환은 *effective* 리스트의
첫 항목을 사용 — per-app 오버라이드가 적용된 앱은 자기 채널/유저로 안내됩니다.
메시지 템플릿 자체는 글로벌로 유지.

`SYSTEM_MESSAGE`는 운영 정책(보안·컴플라이언스)이라 일관성을 위해 **글로벌
전용**입니다. per-app 오버라이드 없음.

## 모델 매트릭스

| 용도 | OpenAI | Bedrock | xAI (Grok) |
|------|--------|---------|------------|
| 텍스트 + tool calling | `gpt-4o-mini`, `gpt-4o`, `gpt-5-*`, `o1/o3/o4` | `us.anthropic.claude-opus-4-6-v1`, `us.anthropic.claude-sonnet-4-5-...`, `amazon.nova-pro-v1:0` | `grok-4-1-fast-reasoning`, `grok-4.20-0309-reasoning`, `grok-4.20-multi-agent-0309` |
| 이미지 생성 | `gpt-image-1`, `dall-e-3` | `amazon.nova-canvas-v1:0`, `amazon.titan-image-generator-v2:0` | `grok-imagine-image`, `grok-imagine-image-pro` |
| 이미지 편집 (`edit_image`) | `gpt-image-1` (멀티 입력), `dall-e-2` (단일+마스크) | — (미지원, `NotImplementedError`) | `grok-imagine-image`, `grok-imagine-image-pro` (xAI 자체 `/v1/images/edits` 엔드포인트) |

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

# edit_image 등 첨부 기반 도구 테스트 — 로컬 파일을 mention 첨부로 시뮬레이션
python localtest.py --attach ./cat.png "이 사진에 모자를 씌워줘"
python localtest.py --attach a.png --attach b.png "두 이미지를 합쳐줘"

# 테스트 (420 테스트)
python -m pytest --cov=src --cov-report=term-missing
python -m pytest tests/test_handlers_message.py -v                  # 메시지 흐름
python -m pytest tests/test_handlers_reactions.py -v                # reaction 흐름
python -m pytest tests/llms/test_bedrock.py -v                      # provider 단위
python -m pytest tests/tools/test_web.py::test_fetch_webpage_jina_happy_path -v   # 단일 케이스
```

`.env.local` 은 `src/config.py` 가 python-dotenv 로 자동 로드합니다. `SLACK_BOT_TOKEN` 이 placeholder 이면 `localtest.py` 가 Slack 호출을 stub 으로 대체하고 `generate_image`/`edit_image` 결과물은 `./.uploads/` 에 파일로 저장됩니다. `--attach` 로 전달한 로컬 파일은 가짜 `https://files.slack.com/local/...` URL 로 매핑되어 SSRF 가드를 통과하면서 디스크에서 직접 읽힙니다 — 실제 Slack URL fetch 는 영향받지 않습니다.

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
app.py                       Lambda entrypoint (lambda_handler만 — serverless contract)
src/
├── runtime.py               싱글톤 (LLM/DDB/SSM/Lambda 클라이언트, Bolt 캐시) + accessors + settings + logger
├── router.py                receiver path + worker path + per-app Bolt 캐시
├── handlers/
│   ├── message.py           _process — app_mention/DM (allowlist, agent, streaming, history)
│   └── reactions.py         _process_reaction + REACTION_HANDLERS dict + 핸들러 (현재 :x: → chat.delete)
├── agent.py                 Agent 루프 (native function calling 반복)
├── credentials.py           SSM 기반 멀티테넌트 시크릿 캐시
├── app_metadata.py          app:{api_app_id} DDB 행 — 자동 등록 + per-app override
├── dedup.py                 DDB 조건부 put 중복 제거 + 스레드 메모리
├── slack_helpers.py         메시지 분할·스트리밍·사용자 캐시
├── config.py                Settings (env → dataclass, lazy validation)
├── logging_utils.py         구조화 JSON 로깅 + request_id
├── llms/                    LLM provider 패키지 (OpenAI · xAI · Bedrock 분기)
└── tools/                   Tool 패키지 (@tool 데코레이터로 self-register)
```

각 모듈 책임과 cross-module 호출 규약은 [CLAUDE.md](CLAUDE.md), 깊은 architecture는 [docs/architecture.md](docs/architecture.md)를 보세요.

테스트는 소스 구조를 미러링: `tests/test_router.py`, `tests/test_handlers_message.py`, `tests/test_handlers_reactions.py`, `tests/llms/`, `tests/tools/`.

## 문서 모음

- **[CLAUDE.md](CLAUDE.md)** — AI agent(Claude Code)를 위한 invariant + 깨지기 쉬운 부분
- **[docs/architecture.md](docs/architecture.md)** — 멀티테넌트 모델, receiver/worker split, dedup, streaming, LLM provider 설계 등 깊은 자료
- **[docs/operations.md](docs/operations.md)** — `scripts/apps.py` 운영 CLI, ACL/persona 시나리오, 시크릿 로테이션, 트러블슈팅
- **[docs/reactions.md](docs/reactions.md)** — `:x:` 권한 모델, 새 reaction 추가 방법, 필요한 Slack scope
- **[docs/extending.md](docs/extending.md)** — 새 tool / LLM provider 추가 절차

## 아키텍처 요약

```
┌────────────────┐  POST /slack/events
│ Slack workspace│──────────────────┐
└────────────────┘                  ▼
                ┌────────────────────────────────────┐
                │ API Gateway → Lambda (app.py)      │
                │ ├─ X-Slack-Retry-Num early-return  │
                │ └─ src.router._route_request       │
                │     ├─ parse → api_app_id          │
                │     ├─ SSM lookup (signing+token)  │
                │     └─ per-app cached Bolt App     │
                └─────────────┬──────────────────────┘
            receiver path     │   ack + lambda:Invoke (async, _worker=True)
                              ▼
                ┌────────────────────────────────────┐
                │ Lambda async self-invoke           │
                │ src.router._process_worker         │
                │   ├─ event.type == reaction_added? │
                │   │   → handlers.reactions         │
                │   └─ otherwise                     │
                │       → handlers.message._process  │
                └─────────────┬──────────────────────┘
                              ▼
                  ┌─────────────────────────────────┐
                  │ Agent loop (native func call)   │
                  │  LLM.chat(messages, tools=reg)  │
                  │   ↓ tool_calls?                 │
                  │  ToolExecutor.execute           │
                  │   ↓ role=tool result            │
                  │  (loop ≤ AGENT_MAX_STEPS)       │
                  │  streaming chat_update          │
                  └────────────┬────────────────────┘
                   ┌───────────┼────────────────┐
                   ▼           ▼                ▼
            ┌──────────┐  ┌──────────┐  ┌─────────────┐
            │ OpenAI   │  │ Bedrock  │  │ Slack Web   │
            │ Chat/Vis │  │ Msg/Conv │  │ API (tools) │
            └──────────┘  └──────────┘  └─────────────┘
                                ▲
                                │
                         ┌──────┴──────┐
                         │ DynamoDB    │
                         │ dedup/ctx/  │
                         │ app registry│
                         └─────────────┘
```

전체 플로우 다이어그램과 각 단계 설명은 [docs/architecture.md](docs/architecture.md).

## 확장하기

- **새 tool**: `src/tools/<name>.py` + `@tool(default_registry, ...)` + `src/tools/__init__.py` import 한 줄. 자세한 절차는 [docs/extending.md](docs/extending.md).
- **새 LLM provider**: `src/llms/<name>.py` + `LLMProvider` Protocol 구현 + `src/llms/factory.py` 분기. [docs/extending.md](docs/extending.md).
- **새 reaction handler**: `src/handlers/reactions.py`에 `_handle_reaction_<name>` 함수 + `REACTION_HANDLERS` dict 한 줄. [docs/reactions.md](docs/reactions.md).
