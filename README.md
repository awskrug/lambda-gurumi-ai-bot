# lambda-gurumi-ai-bot

AWS Lambda, API Gateway, DynamoDB, Amazon Bedrock AI 모델을 활용한 서버리스 챗봇입니다. Slack과 Kakao 메신저를 지원합니다.

![Gurumi Bot](images/gurumi-bot.png)

## 주요 기능

- **서버리스 아키텍처**: AWS Lambda와 API Gateway 기반
- **대화 히스토리 관리**: DynamoDB를 통한 컨텍스트 유지
- **AI 기능**: Amazon Bedrock (Claude 모델) 기반 응답 생성
- **Slack 통합**: 메시지 스레딩 및 멘션 지원
- **Kakao 봇 통합**: REST API 기반 연동
- **채널 기반 접근 제어**: 허용된 채널만 응답
- **사용자 쓰로틀링**: 남용 방지를 위한 요청 제한
- **응답 스트리밍**: 긴 응답 분할 전송으로 사용자 경험 개선

## 설치

```bash
# Python 3.12 설치
brew install python@3.12

# Serverless Framework 설치
npm install -g serverless@3.38.0

# 플러그인 설치
sls plugin install -n serverless-python-requirements
sls plugin install -n serverless-dotenv-plugin

# Python 의존성 설치
python -m pip install --upgrade -r requirements.txt
```

## 설정

### Slack 앱 설정

[Slack Bolt 시작 가이드](https://slack.dev/bolt-js/tutorial/getting-started)를 참고하여 Slack 앱을 생성합니다.

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
```

#### Event Subscriptions - Subscribe to bot events

```text
app_mention
message.im
```

### 환경 변수 설정

```bash
cp .env.example .env.local
```

#### 필수 설정

| 변수명 | 설명 |
|--------|------|
| `SLACK_BOT_TOKEN` | Slack Bot OAuth 토큰 (`xoxb-xxxx`) |
| `SLACK_SIGNING_SECRET` | Slack 요청 서명 검증용 시크릿 |
| `AGENT_ID` | Bedrock Agent ID |
| `AGENT_ALIAS_ID` | Bedrock Agent Alias ID |

#### 선택적 설정

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `AWS_REGION` | `us-east-1` | AWS 리전 |
| `DYNAMODB_TABLE_NAME` | `gurumi-ai-bot-dev` | DynamoDB 테이블명 |
| `KAKAO_BOT_TOKEN` | `None` | Kakao 봇 인증 토큰 |
| `ALLOWED_CHANNEL_IDS` | `None` | 허용 채널 ID (쉼표 구분) |
| `ALLOWED_CHANNEL_MESSAGE` | 영문 메시지 | 비허용 채널 응답 메시지 |
| `PERSONAL_MESSAGE` | 일반 AI 어시스턴트 | AI 페르소나 설정 |
| `SYSTEM_MESSAGE` | `None` | 추가 시스템 지시사항 |
| `MAX_LEN_SLACK` | `2000` | Slack 메시지 최대 길이 |
| `MAX_LEN_BEDROCK` | `4000` | Bedrock 컨텍스트 최대 길이 |
| `MAX_THROTTLE_COUNT` | `100` | 사용자별 요청 제한 수 |
| `SLACK_SAY_INTERVAL` | `0` | 메시지 전송 간격 (초) |
| `BOT_CURSOR` | `:robot_face:` | 로딩 표시 이모지 |

## 배포

```bash
# 기본 배포 (dev 스테이지)
sls deploy --region us-east-1

# 프로덕션 배포
sls deploy --stage prod --region us-east-1

# 배포 제거
sls remove --region us-east-1
```

## 테스트

### Slack URL 검증 테스트

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "token": "Jhj5dZrVaK7ZwHHjRyZWjbDl",
    "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P",
    "type": "url_verification"
  }' \
  https://xxxx.execute-api.us-east-1.amazonaws.com/dev/slack/events
```

### Kakao 봇 테스트

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_KAKAO_BOT_TOKEN" \
  -d '{
    "query": "안녕하세요?"
  }' \
  https://xxxx.execute-api.us-east-1.amazonaws.com/dev/kakao/events
```

### Bedrock 직접 테스트

```bash
cd bin/bedrock

# Bedrock Agent 테스트
python invoke_agent.py -p "프롬프트 입력"

# Claude 3 모델 직접 호출
python invoke_claude_3.py -p "프롬프트 입력"

# 이미지 생성 (Stable Diffusion)
python invoke_stable_diffusion.py -p "이미지 생성 프롬프트"

# Knowledge Base 쿼리
python invoke_knowledge_base.py -p "지식 베이스 쿼리"
```

## 아키텍처

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Slack     │────▶│ API Gateway │────▶│   Lambda    │
│   Kakao     │     │             │     │  (handler)  │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                    ┌──────────────────────────┼──────────────────────────┐
                    │                          │                          │
                    ▼                          ▼                          ▼
             ┌─────────────┐           ┌─────────────┐           ┌─────────────┐
             │  DynamoDB   │           │   Bedrock   │           │     S3      │
             │  (Context)  │           │   (Agent)   │           │  (Storage)  │
             └─────────────┘           └─────────────┘           └─────────────┘
```

## 프로젝트 구조

```
.
├── handler.py              # Lambda 핸들러 및 핵심 로직
├── serverless.yml          # Serverless Framework 설정
├── requirements.txt        # Python 의존성
├── .env.example            # 환경 변수 예시
├── .env.local              # 환경 변수 (gitignore)
├── images/
│   └── gurumi-bot.png      # 프로젝트 이미지
├── bin/
│   └── bedrock/            # Bedrock 테스트 스크립트
│       ├── invoke_agent.py
│       ├── invoke_claude_3.py
│       ├── invoke_claude_3_image.py
│       ├── invoke_knowledge_base.py
│       ├── invoke_stable_diffusion.py
│       └── converse_stream.py
└── .github/
    └── workflows/
        └── push.yml        # CI/CD 파이프라인
```

## 참고 자료

- [AWS Bedrock Documentation](https://docs.aws.amazon.com/bedrock/)
- [Slack Bolt Framework](https://slack.dev/bolt-js/)
- [Serverless Framework](https://www.serverless.com/)
