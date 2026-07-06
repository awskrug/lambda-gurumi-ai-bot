# Operations

운영자(operator)를 위한 가이드입니다. 봇이 어떻게 동작하는지의 설계는 [docs/architecture.md](architecture.md)를, 새 tool/provider 추가는 [docs/extending.md](extending.md)를 보세요.

## 새 Slack 앱 추가

이 Lambda는 단일 배포로 여러 Slack 앱을 서빙합니다. 새 앱을 등록하려면:

### 1. Slack 앱 콘솔에서 앱 생성

Slack API 콘솔 (https://api.slack.com/apps) 에서 새 앱을 만들고 다음 설정:

- **OAuth & Permissions → Bot Token Scopes**
  - `chat:write` — 메시지 전송 + 자기 메시지 삭제
  - `app_mentions:read`, `im:history`, `im:read`, `im:write` — 멘션/DM 수신
  - `users:read` — 사용자 이름 lookup
  - `files:read` — 첨부 파일 읽기 (`read_attached_images`/`read_attached_document`)
  - `files:write` — 이미지 업로드 (`generate_image`/`edit_image`/`attach_image_from_url` tool, `/img-gpt`·`/img-xai`, 이미지 생성 reaction)
  - `assistant:write` — 스레드 status 인디케이터
  - **(reaction 기능 사용 시)** `reactions:read` + `channels:history` / `groups:history` / `im:history` / `mpim:history` (봇이 동작하는 채널 종류에 맞게)
- **Event Subscriptions**
  - Request URL: `https://{your-api-gateway}/slack/events`
  - Subscribe to bot events: `app_mention`, `message.im`, **(reaction 기능 사용 시)** `reaction_added`
- **(slash command 사용 시) Slash Commands** — `/img-gpt`, `/img-xai` 두 명령을 생성, Request URL: `https://{your-api-gateway}/slack/command` (`commands` scope는 명령 생성 시 자동 추가)
- **App Home → Show Tabs → Allow users to send Slash commands and messages from the messages tab**
- 워크스페이스에 설치/재인증

### 2. SSM Parameter Store에 시크릿 등록

운영 CLI(권장 — 시크릿 마스킹 입력, 값 출력 없음, `delete` 시 `app_id` 재입력 확인):

```bash
# 인터랙티브 (getpass 프롬프트)
python scripts/apps.py set A0123ABC

# 스크립트용 (env var pointer — shell history 노출 없음)
SIG=$(cat ./signing.txt) TOK=$(cat ./bot.txt) \
  python scripts/apps.py set A0123ABC \
  --signing-secret-env=SIG --bot-token-env=TOK

# auth.test 자동 호출 안 하려면 (오프라인 환경 등)
python scripts/apps.py set A0123ABC --no-verify
```

또는 직접 awscli (마스킹·확인 절차 없음, 디버깅 시에만 권장):

```bash
aws ssm put-parameter --type SecureString \
  --name /gurumi-bot/apps/A0123ABC/signing_secret --value "$SLACK_SIGNING_SECRET"
aws ssm put-parameter --type SecureString \
  --name /gurumi-bot/apps/A0123ABC/bot_token --value "$SLACK_BOT_TOKEN"
```

`api_app_id`는 Slack 앱 콘솔의 **Basic Information → App ID** (예: `A0123ABCXYZ`).

### 3. 첫 이벤트 도착 → 자동 등록

SSM에 시크릿이 있는 상태에서 Slack 이벤트가 들어오면 봇이 자동으로 처리하고, dedup 통과 후 `app:{api_app_id}` DynamoDB 행을 lazy upsert합니다. 별도 등록 절차 불필요.

`scripts/apps.py list`로 확인:

```bash
$ python scripts/apps.py list
APP_ID         TEAM             BOT          NAME                       SSM    DDB    LAST_SEEN
A0123ABCXYZ    Acme Corp        gurumi       (auto)                     ✓      ✓      2025-01-15T10:30Z
```

## Slack 앱 관리 — `scripts/apps.py`

운영 CLI가 시크릿 + 메타데이터 + ACL/persona 오버라이드를 한 곳에서 관리합니다.

### 앱 목록/상세

```bash
python scripts/apps.py list                  # 등록된 모든 앱 + SSM/DDB 상태
python scripts/apps.py get A0123ABC          # 단일 앱 상세 (값은 출력 안 함)
```

### 시크릿 설정/삭제

```bash
python scripts/apps.py set A0123ABC          # getpass 인터랙티브
python scripts/apps.py delete A0123ABC       # app_id 재입력 후 SSM+DDB 양쪽 삭제

# 스크립트용
python scripts/apps.py set A0123ABC \
  --signing-secret-env=SIG --bot-token-env=TOK
```

`delete`는 `app_id`를 다시 타이핑하라고 요구합니다. 이는 muscle-memory 삭제로 SSM 시크릿과 DDB 메타데이터가 동시에 사라지는 것을 막기 위한 의도된 마찰 — undo가 없습니다.

`--yes` 플래그가 있지만 이는 **스크립트 자동화용**입니다. 평소 운영에서는 사용하지 마세요.

### 앱 식별 (apps list NAME 컬럼)

```bash
# Slack auth.test 호출해서 team/bot 정보 갱신 (apps list 가독성)
python scripts/apps.py refresh A0123ABC

# 운영자 라벨 (refresh 결과보다 우선)
python scripts/apps.py name set A0123ABC "Production - Acme"
python scripts/apps.py name unset A0123ABC   # 자동 채워진 team/bot으로 복귀
```

`apps set`은 기본적으로 `auth.test`를 호출해 즉시 `team_name`/`bot_user_name`을 채웁니다. `--no-verify`로 스킵 가능 (오프라인 환경).

## Per-app override

세 가지 deployment-wide 기본값을 *각 앱별로* 덮어쓸 수 있습니다.

| 환경 변수 | 덮어쓸 수 있는 속성 | CLI 명령 |
|-----------|--------------------|----------|
| `ALLOWED_CHANNEL_IDS` | `allowed_channel_ids` | `acl set --channels=...` |
| `ALLOWED_USER_IDS` | `allowed_user_ids` | `acl set --users=...` |
| `PERSONA_MESSAGE` | `persona_message` | `persona set ...` |

### ACL (channel/user allowlist)

```bash
python scripts/apps.py acl get A0123ABC                       # 현재 상태 (per-app + 글로벌 + effective)

# 채널 제한 (이 앱만)
python scripts/apps.py acl set A0123ABC --channels=C1,C2

# 명시적 "모두 허용" — 글로벌이 restrictive해도 이 앱은 모든 채널/유저 허용
python scripts/apps.py acl set A0123ABC --channels=""
python scripts/apps.py acl set A0123ABC --users=""

# 유저 제한
python scripts/apps.py acl set A0123ABC --users=U1,U2

# 오버라이드 제거 → 글로벌 env var로 복귀
python scripts/apps.py acl unset A0123ABC --channels --users
```

### Persona (PERSONA_MESSAGE)

```bash
python scripts/apps.py persona get A0123ABC                   # 현재 상태

# 짧은 텍스트
python scripts/apps.py persona set A0123ABC "당신은 친근한 어시스턴트"

# 멀티라인은 파일에서
python scripts/apps.py persona set A0123ABC --from-file persona.txt

# 명시적 "페르소나 없음" — 글로벌이 설정되어 있어도 이 앱은 페르소나 미적용
python scripts/apps.py persona set A0123ABC ""

# 오버라이드 제거 → 글로벌 env var로 복귀
python scripts/apps.py persona unset A0123ABC
```

### 3-state 계약 — 빈값의 의미

`""` / `[]`는 단순한 "없음"이 아니라 **명시적 오버라이드**입니다:

| 상태 | 의미 |
|------|------|
| 속성 *없음* | 글로벌 env var 사용 (기본) |
| 속성 = `[C1, C2]` / `"text"` | per-app 값 사용, 글로벌 무시 |
| 속성 = `[]` / `""` | "이 앱은 명시적으로 모두 허용" 또는 "이 앱은 페르소나 없음" — 글로벌 무시 |

### 차단 메시지 치환

`ALLOWED_CHANNEL_MESSAGE` / `ALLOWED_USER_MESSAGE`의 `{}`는 *effective* 리스트의 첫 항목을 사용합니다. 즉 per-app override가 적용된 앱은 자기 채널/유저로 안내됩니다 (글로벌 채널/유저가 아니라). 메시지 *템플릿 자체*는 글로벌로 유지.

### `SYSTEM_MESSAGE`는 per-app override 불가

`SYSTEM_MESSAGE`는 운영 정책(보안·컴플라이언스)이라 의도적으로 글로벌 전용입니다. 한 앱만 정책을 약화시키는 것을 막기 위함. `PERSONA_MESSAGE`(답변 스타일/톤)는 per-app 가능.

## 시크릿 로테이션

### 정상 로테이션

1. SSM에 새 값 put:
   ```bash
   python scripts/apps.py set A0123ABC   # 기존 SecureString을 덮어씀
   ```
2. `SSM_CACHE_TTL_SECONDS` (기본 5분) 내에 모든 warm Lambda 컨테이너가 새 값으로 reload
3. 컨테이너 재시작 불필요 — `_bolt_apps` 캐시가 secret 튜플을 value로 갖고 있어 자동 재빌드

### 즉시 반영이 필요하면

옵션 A: TTL 짧게 (예: `SSM_CACHE_TTL_SECONDS=60`) → 5분 대신 1분
옵션 B: Lambda 재배포 (warm 컨테이너 모두 폐기)
옵션 C: 무시하고 5분 기다림 (대부분 케이스)

### 시크릿 누출 시

1. **즉시 새 시크릿 생성** (Slack 콘솔에서 regenerate)
2. SSM에 새 값 put (`apps set`)
3. Slack 콘솔에서 옛 시크릿 revoke
4. CloudWatch Insights에서 `request.unknown_app` 로그 모니터링 (외부 위조 시도 흔적)

코드 수정만으로는 부족 — **반드시 시크릿 자체를 rotate** 하세요.

## 트러블슈팅

### `request.unknown_app` 로그가 보이는데 새 앱이 응답하지 않음

원인: SSM에 시크릿이 없거나 (이름 오타 / 잘못된 prefix), Lambda IAM이 SSM 접근 권한이 없음.

확인:

```bash
# SSM에 실제로 있는지
aws ssm get-parameter \
  --name /gurumi-bot/apps/A0123ABC/signing_secret --with-decryption

# Lambda IAM 정책 확인
aws iam get-role-policy --role-name lambda-gurumi-bot ...
```

해결: `python scripts/apps.py set A0123ABC` 다시 실행. `SSM_CACHE_TTL_SECONDS` 내 반영.

### `worker.unknown_app` 로그

Receiver가 SSM 시크릿을 못 찾았는데 worker invoke가 fired된 케이스 (이론적으로는 없음 — receiver가 먼저 unknown_app 처리). 또는 receiver path 진입 후 worker invoke 사이에 SSM에서 시크릿이 삭제됨.

해결: 시크릿 재등록.

### 봇이 같은 메시지에 두 번 응답함

Dedup이 작동하지 않는 의미. 원인 후보:

- DynamoDB IAM에 `dynamodb:PutItem` 권한 없음 → reserve 실패하면 `_process`가 `proceeding without it` 경고 후 진행
- DynamoDB 테이블 이름이 잘못됨 → 같은 권한 에러 패턴
- `id` 키 schema가 잘못됨 (해시키 != `id`)

확인:

```bash
aws dynamodb describe-table --table-name lambda-gurumi-bot-dev
```

해결: 테이블 schema가 `id` 해시키 + `expire_at` TTL이어야 함. `serverless.yml` 의 CloudFormation 정의가 source of truth.

### 채널 allowlist를 설정했는데 DM이 막힘

DM은 의도적으로 **채널 allowlist 대상이 아닙니다**. `ALLOWED_CHANNEL_IDS`는 채널/그룹에만 적용됩니다 — DM 채널 ID(`D...`)는 allowlist에 enroll되지 않으므로 enforce했다면 모든 DM이 막힘. 코드는 `is_dm=True`일 때 채널 allowlist 체크를 건너뜁니다.

DM도 막고 싶으면 `ALLOWED_USER_IDS`를 사용하세요 (이건 채널·DM 양쪽 적용).

### `apps list`에서 NAME 컬럼이 비어 있음

원인: `auth.test`가 호출되지 않았거나 실패. `apps set`은 기본 호출하지만 `--no-verify`로 스킵했거나 Slack 도달 실패 시.

해결:

```bash
python scripts/apps.py refresh A0123ABC
```

### Stream이 흘러나오는데 갑자기 끊기고 새 메시지로 이어짐

`MAX_LEN_SLACK` 임계값에 가까워지면 `StreamingMessage`가 새 `chat.postMessage`로 roll합니다. 정상 동작.

### 긴 응답이 한 곳에 다 안 보임

응답이 `MAX_LEN_SLACK`을 초과하면 `MessageFormatter.split_message`가 코드펜스 → 문단 → 문장 → hard slice 우선순위로 분할. 첫 chunk는 placeholder 메시지 update, 나머지는 thread 새 메시지.

## IAM / 권한

### Lambda 런타임 role

`serverless.yml`이 정의:

- `dynamodb:GetItem/PutItem/Query/UpdateItem` on table + `user-index` GSI
- `ssm:GetParameters` on `{SSM_PARAMS_PREFIX}/*` (배포 prefix와 일치)
- `kms:Decrypt` on the SSM SecureString의 KMS 키 (default key 사용 시 자동, custom key는 명시 필요)
- `bedrock:InvokeModel*`, `bedrock:Converse*` (Bedrock provider 사용 시)
- `lambda:InvokeFunction` on **자기 자신의 ARN** — `_enqueue_worker`의 self-invoke

`dynamodb:UpdateItem` 누락 시 `AppMetadataStore.record()`가 silent fail → `app:` 행이 안 생기고 per-app override가 글로벌로 fallback. `dedup:`/`ctx:` 행은 `put_item` 사용이라 영향 없음 → 알아채기 어려운 회귀.

### OIDC role (배포용, `.github/aws-role/`)

GitHub Actions에서 `serverless deploy`를 위해 사용. Lambda 런타임 role과 별개. 자세한 절차는 [README의 배포 섹션](../README.md#배포)을 보세요.

## Reaction 기능 운영

`:x:`(봇 글 삭제)와 `:img-gpt:`/`:img-xai:`(이미지 생성) reaction을 사용하려면 추가 Slack OAuth scope와 Event Subscription이 필요합니다. `img-gpt`/`img-xai`는 워크스페이스에 custom emoji로 등록되어 있어야 합니다. 자세한 설정과 권한 모델은 [docs/reactions.md](reactions.md)를 보세요.

## 배포

배포 절차는 [README의 배포 섹션](../README.md#배포)에 정리되어 있습니다.
