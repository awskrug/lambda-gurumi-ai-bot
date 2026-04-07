# lambda-gurumi-ai-bot

AWS Lambda, Amazon Bedrock, S3 Vectors를 활용한 서버리스 AI 챗봇입니다. RAG(Retrieval-Augmented Generation) 기반으로 문서를 검색하여 답변합니다.

![Gurumi Bot](images/gurumi-bot.png)

## 주요 기능

- **RAG 지원**: S3 Vectors + Bedrock Knowledge Base 기반 문서 검색 및 답변
- **서버리스 아키텍처**: AWS Lambda + API Gateway + DynamoDB
- **대화 히스토리**: DynamoDB를 통한 스레드 컨텍스트 유지 (1시간 TTL)
- **Slack 통합**: 앱 멘션, 다이렉트 메시지, 이모지 리액션 지원
- **채널 접근 제어**: 허용된 채널 화이트리스트
- **사용자 쓰로틀링**: 남용 방지를 위한 요청 제한
- **응답 분할**: 긴 응답을 코드 블록/문단 단위로 분할 전송

## 아키텍처

```
┌──────────┐     ┌─────────────┐     ┌─────────────┐
│  Slack   │────▶│ API Gateway │────▶│   Lambda    │
└──────────┘     └─────────────┘     └──────┬──────┘
                                            │
              ┌─────────────────────────────┼──────────────────┐
              │                             │                  │
              ▼                             ▼                  ▼
       ┌─────────────┐           ┌──────────────────┐  ┌─────────────┐
       │  DynamoDB   │           │  Bedrock Agent   │  │     S3      │
       │  (Context)  │           │       │          │  │ (Documents) │
       └─────────────┘           │  Knowledge Base  │  └──────┬──────┘
                                 │       │          │         │
                                 │  S3 Vectors      │◀────────┘
                                 │  (Embeddings)    │  Titan Embeddings V2
                                 └──────────────────┘
```

## 설치

```bash
# Python 3.12 설치
brew install python@3.12

# Serverless Framework 설치
npm install -g serverless@3.38.0

# 프로젝트 의존성 설치
npm install
sls plugin install -n serverless-python-requirements
sls plugin install -n serverless-dotenv-plugin
python -m pip install --upgrade -r requirements.txt
```

## 설정

### Slack 앱 설정

[Slack Bolt for Python 시작 가이드](https://slack.dev/bolt-python/tutorial/getting-started)를 참고하여 Slack 앱을 생성합니다.

#### OAuth & Permissions - Bot Token Scopes

```text
app_mentions:read
channels:history
channels:join
channels:read
chat:write
files:read
files:write
im:read
im:write
reactions:read
users:read
```

#### Event Subscriptions - Subscribe to bot events

```text
app_mention
message.im
reaction_added
```

### 환경 변수

```bash
cp .env.example .env.local
```

#### 필수 설정 (GitHub Secrets)

| 변수명 | 설명 |
|--------|------|
| `SLACK_BOT_TOKEN` | Slack Bot OAuth 토큰 (`xoxb-xxxx`) |
| `SLACK_SIGNING_SECRET` | Slack 요청 서명 검증용 시크릿 |
| `NOTION_TOKEN` | Notion Integration API 키 ([생성](https://www.notion.so/my-integrations)) |

> `AGENT_ID`/`AGENT_ALIAS_ID`는 CloudFormation에서 자동 관리됩니다.

#### 선택적 설정

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `AWS_REGION` | `us-east-1` | AWS 리전 |
| `DYNAMODB_TABLE_NAME` | `gurumi-ai-bot-dev` | DynamoDB 테이블명 |
| `ALLOWED_CHANNEL_IDS` | `None` | 허용 채널 ID (쉼표 구분) |
| `PERSONAL_MESSAGE` | 일반 AI 어시스턴트 | AI 페르소나 설정 |
| `SYSTEM_MESSAGE` | `None` | 추가 시스템 지시사항 |
| `MAX_LEN_SLACK` | `2000` | Slack 메시지 최대 길이 |
| `MAX_LEN_BEDROCK` | `4000` | Bedrock 컨텍스트 최대 길이 |
| `MAX_THROTTLE_COUNT` | `100` | 사용자별 요청 제한 수 |
| `BOT_CURSOR` | `:robot_face:` | 로딩 표시 이모지 |
| `REACTION_EMOJIS` | `refund-done` | 허용 이모지 리액션 (쉼표 구분) |

## 배포

```bash
# 기본 배포 (dev 스테이지)
sls deploy --region us-east-1

# 프로덕션 배포
sls deploy --stage prod --region us-east-1

# 배포 제거
sls remove --region us-east-1
```

### RAG 문서 추가

배포 후 S3 버킷의 `documents/` 프리픽스에 문서를 업로드하고 동기화합니다.

```bash
# 문서 업로드 (PDF, TXT, MD, HTML, DOCX, CSV 지원)
aws s3 cp my-document.pdf s3://gurumi-ai-bot-{account-id}/documents/

# Knowledge Base 동기화
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KB_ID> \
  --data-source-id <DS_ID>
```

### 문서 동기화

두 가지 자동 동기화 워크플로우가 있으며, GitHub Variables로 활성화합니다.

| 워크플로우 | 소스 | 활성화 변수 |
|-----------|------|------------|
| `sync-notion.yml` | Notion 페이지 → Markdown | `ENABLE_SYNC_NOTION=true` |
| `sync-awsdocs.yml` | AWS 공식 PDF (19개 서비스) | `ENABLE_SYNC_AWSDOCS=true` |

AWS 문서 목록은 `scripts/awsdocs/docs.txt`에서 관리합니다.

### CI/CD

GitHub Actions (`push.yml`)로 `main` 브랜치 푸시 시 자동 배포됩니다.

## 테스트

```bash
# Slack URL 검증
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"token": "test", "challenge": "test_challenge", "type": "url_verification"}' \
  https://your-api-url/dev/slack/events

# Bedrock Agent 직접 테스트
cd scripts/bedrock
python invoke_agent.py -p "프롬프트 입력"
python invoke_knowledge_base.py -p "지식 베이스 쿼리"
```

## 참고 자료

- [AWS Bedrock Documentation](https://docs.aws.amazon.com/bedrock/)
- [Amazon S3 Vectors](https://aws.amazon.com/s3/features/vectors/)
- [Slack Bolt for Python](https://slack.dev/bolt-python/)
- [Serverless Framework](https://www.serverless.com/)
