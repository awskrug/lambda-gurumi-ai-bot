# CLAUDE.md

이 파일은 Claude Code (claude.ai/code)가 이 저장소의 코드 작업 시 참고하는 가이드입니다.

## 프로젝트 개요

AWS Lambda, Amazon Bedrock AI 모델, DynamoDB를 활용한 서버리스 챗봇 애플리케이션입니다. Slack과 Kakao 메신저를 지원합니다.

## 주요 명령어

### 초기 설정

```bash
# Python 3.12 설치 (미설치 시)
brew install python@3.12

# Serverless Framework 설치
npm install -g serverless@3.38.0

# 프로젝트 의존성 설치
npm install
sls plugin install -n serverless-python-requirements
sls plugin install -n serverless-dotenv-plugin
python -m pip install --upgrade -r requirements.txt

# 환경 변수 설정
cp .env.example .env.local
# .env.local 파일에 필요한 인증 정보 및 설정 입력
```

### 배포

```bash
# AWS 배포 (기본 스테이지: dev)
sls deploy --region us-east-1

# 특정 스테이지로 배포
sls deploy --stage prod --region us-east-1

# 배포 제거
sls remove --region us-east-1
```

### Bedrock 통합 테스트

```bash
# examples/bedrock/ 디렉토리의 예제 스크립트 사용
cd examples/bedrock
python invoke_agent.py -p "프롬프트 입력"
python invoke_claude_3.py -p "프롬프트 입력"
python invoke_stable_diffusion.py -p "이미지 생성 프롬프트"
python invoke_knowledge_base.py -p "지식 베이스 쿼리"
```

### 로컬 테스트

```bash
# Slack URL 검증 테스트
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"token": "test", "challenge": "test_challenge", "type": "url_verification"}' \
  https://your-api-url/dev/slack/events

# Kakao 봇 엔드포인트 테스트
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_KAKAO_BOT_TOKEN" \
  -d '{"query": "Hello"}' \
  https://your-api-url/dev/kakao/events
```

## 아키텍처

### 핵심 컴포넌트

#### 1. handler.py - Lambda 함수 진입점

| 함수/핸들러 | 설명 |
|-------------|------|
| `lambda_handler` | Slack 이벤트 처리 (앱 멘션, 다이렉트 메시지) |
| `kakao_handler` | Kakao 봇 요청 처리 |
| `conversation` | 대화 처리 및 AI 응답 생성 |
| `handle_mention` | 앱 멘션 이벤트 핸들러 |
| `handle_message` | 다이렉트 메시지 이벤트 핸들러 |
| `handle_reaction_added` | 이모지 리액션 이벤트 핸들러 |
| `process_refund_done` | 환불 완료 처리 (계좌 마스킹, 환불일시 추가) |
| `mask_account_number` | 계좌번호 마스킹 (앞 4자리, 뒤 2자리만 표시) |

#### 2. 주요 클래스

| 클래스 | 역할 |
|--------|------|
| `Config` | 환경 변수 기반 설정 관리 (17개 설정 항목) |
| `DynamoDBManager` | 대화 컨텍스트 저장/조회, 사용자별 쓰로틀링 카운트 |
| `MessageFormatter` | 메시지 분할 (코드 블록, 문단 단위) |
| `SlackManager` | Slack 메시지 업데이트, 스레드 히스토리 조회 |
| `BedrockManager` | Bedrock Agent 호출, 프롬프트 생성 |

#### 3. AWS 리소스 (serverless.yml)

| 리소스 | 이름 패턴 | 용도 |
|--------|-----------|------|
| Lambda Functions | `mention`, `kakao` | Slack/Kakao 핸들러 |
| DynamoDB Table | `gurumi-ai-bot-{stage}` | TTL 기반 컨텍스트 저장 |
| S3 Bucket | `gurumi-ai-bot-{account-id}` | 파일 저장 |
| IAM Permissions | - | DynamoDB, Bedrock 접근 |

### 데이터 흐름

#### Slack 통합

1. HTTP POST `/slack/events`로 이벤트 수신
2. Signing Secret으로 요청 검증
3. 중복 이벤트 감지 (`client_msg_id` 기반 DynamoDB 체크)
4. 사용자별 쓰로틀링 체크 (`MAX_THROTTLE_COUNT`)
5. 스레드 대화 관리 및 컨텍스트 유지
6. 긴 응답은 청크 단위로 분할 전송

#### 대화 컨텍스트

- DynamoDB에 `thread_ts` 또는 `user_id`를 키로 저장
- 1시간 TTL로 자동 정리 (`expire_at` 속성)
- 스레드 히스토리에서 대화 기록 조회 (`MAX_LEN_BEDROCK` 제한)

#### AI 처리

- Bedrock Agent를 통한 응답 생성
- 프롬프트에 시스템 메시지, 대화 히스토리, 질문 포함
- `<question>` 태그로 사용자 질문 래핑
- `<history>` 태그로 대화 기록 래핑

#### Kakao 통합

- HTTP POST `/kakao/events`로 요청 수신
- Bearer 토큰 인증 (`KAKAO_BOT_TOKEN`)
- 대화 컨텍스트 없이 단일 질의/응답

### 환경 변수

`.env.local` 파일로 관리되는 설정:

#### 필수 설정

| 변수명 | 설명 |
|--------|------|
| `SLACK_BOT_TOKEN` | Slack Bot OAuth 토큰 |
| `SLACK_SIGNING_SECRET` | Slack 요청 서명 검증용 시크릿 |
| `AGENT_ID` | Bedrock Agent ID |
| `AGENT_ALIAS_ID` | Bedrock Agent Alias ID |

#### 선택적 설정

| 변수명 | 기본값 | 설명 |
|--------|--------|------|
| `AWS_REGION` | `us-east-1` | AWS 리전 |
| `DYNAMODB_TABLE_NAME` | `gurumi-ai-bot-dev` | DynamoDB 테이블명 |
| `KAKAO_BOT_TOKEN` | `None` | Kakao 봇 인증 토큰 |
| `ALLOWED_CHANNEL_IDS` | `None` | 허용 채널 ID (쉼표 구분, 모든 채널 허용) |
| `ALLOWED_CHANNEL_MESSAGE` | 영문 메시지 | 비허용 채널 응답 메시지 |
| `PERSONAL_MESSAGE` | 일반 AI 어시스턴트 | AI 페르소나 설정 메시지 |
| `SYSTEM_MESSAGE` | `None` | 추가 시스템 지시사항 |
| `MAX_LEN_SLACK` | `2000` | Slack 메시지 최대 길이 |
| `MAX_LEN_BEDROCK` | `4000` | Bedrock 컨텍스트 최대 길이 |
| `MAX_THROTTLE_COUNT` | `100` | 사용자별 요청 제한 수 |
| `SLACK_SAY_INTERVAL` | `0` | 메시지 전송 간격 (초) |
| `BOT_CURSOR` | `:robot_face:` | 로딩 표시 이모지 |
| `REACTION_EMOJIS` | `refund-done` | 허용 이모지 리액션 (쉼표 구분) |

### 배포 파이프라인

GitHub Actions 워크플로우 (`.github/workflows/push.yml`):

1. main 브랜치 푸시 시 트리거
2. Python 3.12 환경 설정
3. 모든 의존성 설치
4. GitHub Secrets에서 환경 변수 구성
5. AWS IAM 역할 가정
6. Serverless Framework로 배포

## 주요 구현 상세

| 기능 | 설명 |
|------|------|
| 메시지 스레딩 | Slack `thread_ts`로 대화 컨텍스트 유지 |
| 중복 이벤트 방지 | `client_msg_id`를 DynamoDB에 저장하여 중복 처리 방지 |
| 사용자 쓰로틀링 | 사용자별 활성 컨텍스트 수 기반 제한 |
| 에러 처리 | 사용자 친화적 에러 메시지 (한국어) |
| 응답 분할 | 코드 블록과 문단 단위로 긴 응답 분할 |
| 채널 필터링 | 허용된 채널 화이트리스트 지원 |
| 컨텍스트 영속성 | DynamoDB TTL 기반 1시간 자동 정리 |
| 이모지 리액션 처리 | `reaction_added` 이벤트로 특정 동작 트리거 |
| 환불 완료 처리 | `:refund-done:` 이모지로 계좌 마스킹 및 환불일시 추가 |

## 프로젝트 구조

```text
.
├── handler.py              # Lambda 핸들러 및 핵심 로직
├── serverless.yml          # Serverless Framework 설정
├── requirements.txt        # Python 의존성
├── .env.example            # 환경 변수 예시
├── .env.local              # 환경 변수 (gitignore)
├── examples/
│   ├── bedrock/            # Bedrock 예제 스크립트
│   │   ├── invoke_agent.py
│   │   ├── invoke_claude_3.py
│   │   ├── invoke_claude_3_image.py
│   │   ├── invoke_knowledge_base.py
│   │   ├── invoke_stable_diffusion.py
│   │   └── converse_stream.py
│   ├── notion/             # Notion 예제 스크립트
│   │   ├── notion_exporter.py
│   │   └── python_notion_exporter.py
│   └── split.py            # 텍스트 분할 예제
└── .github/
    └── workflows/
        └── push.yml        # CI/CD 파이프라인
```
