# CLAUDE.md

이 파일은 Claude Code가 이 저장소의 코드를 수정할 때 참고하는 개발 가이드입니다.

## 주요 명령어

```bash
# 배포
sls deploy --region us-east-1

# 배포 제거
sls remove --region us-east-1

# Bedrock 테스트
cd scripts/bedrock && python invoke_agent.py -p "프롬프트"
```

## 코드 구조

### handler.py - 단일 파일 구조

모든 핵심 로직이 `handler.py`에 포함되어 있습니다.

#### 클래스

| 클래스 | 역할 | 주요 메서드 |
|--------|------|------------|
| `Config` | 환경 변수 기반 설정 (16개 항목) | `validate()`, `get_reaction_emojis()` |
| `DynamoDBManager` | 컨텍스트 저장/조회, 중복 감지, 쓰로틀링 | `put_context()`, `get_context()`, `count_user_contexts()` |
| `MessageFormatter` | 응답 분할 (코드 블록, 문단, 문장 단위) | `split_message()` |
| `SlackManager` | 메시지 업데이트, 스레드 히스토리 조회 | `update_message()`, `get_thread_history()` |
| `BedrockManager` | Agent 호출, 프롬프트 구성 | `invoke_agent()`, `create_prompt()` |

#### 핸들러/함수

| 함수 | 트리거 | 설명 |
|------|--------|------|
| `lambda_handler` | HTTP POST `/slack/events` | Slack 이벤트 진입점 |
| `handle_mention` | `app_mention` 이벤트 | 앱 멘션 처리 |
| `handle_message` | `message` 이벤트 | 다이렉트 메시지 처리 |
| `handle_reaction_added` | `reaction_added` 이벤트 | 이모지 리액션 처리 |
| `conversation` | 내부 호출 | AI 응답 생성 및 전송 |
| `process_refund_done` | `:refund-done:` 리액션 | 계좌번호 마스킹, 환불일시 추가 |

#### 데이터 흐름

```
Slack 이벤트 → lambda_handler → handle_mention/handle_message
  → 중복 감지 (client_msg_id, DynamoDB)
  → 쓰로틀링 체크 (MAX_THROTTLE_COUNT)
  → conversation()
    → SlackManager.get_thread_history() → 대화 컨텍스트 수집
    → BedrockManager.create_prompt() → 프롬프트 구성
      - PERSONAL_MESSAGE (페르소나)
      - SYSTEM_MESSAGE (시스템 지시)
      - <history> 태그 (대화 기록)
      - <question> 태그 (현재 질문)
    → BedrockManager.invoke_agent() → Bedrock Agent 호출
      - Agent가 Knowledge Base를 자동으로 쿼리 (RAG)
    → MessageFormatter.split_message() → 응답 분할 (MAX_LEN_SLACK)
    → SlackManager.update_message() → Slack 전송
```

### serverless.yml - AWS 리소스

#### CloudFormation 리소스

| 리소스 | 타입 | 이름 패턴 |
|--------|------|-----------|
| DynamoDBTable | `AWS::DynamoDB::Table` | `gurumi-ai-bot-{stage}` |
| S3Bucket | `AWS::S3::Bucket` | `gurumi-ai-bot-{account-id}` |
| S3VectorBucket | `AWS::S3Vectors::VectorBucket` | `gurumi-ai-bot-vectors-{account-id}` |
| S3VectorIndex | `AWS::S3Vectors::Index` | `gurumi-ai-bot-index` (1024dim, cosine, float32) |
| BedrockKBRole | `AWS::IAM::Role` | `lambda-gurumi-ai-bot-kb-role` |
| BedrockKnowledgeBase | `AWS::Bedrock::KnowledgeBase` | `gurumi-ai-bot-kb` |
| BedrockDataSource | `AWS::Bedrock::DataSource` | `gurumi-ai-bot-datasource` |
| BedrockAgentRole | `AWS::IAM::Role` | `lambda-gurumi-ai-bot-agent-role` |
| BedrockAgent | `AWS::Bedrock::Agent` | `gurumi-ai-bot` (Claude Sonnet 4.5, KB 연결 포함) |
| BedrockAgentAlias | `AWS::Bedrock::AgentAlias` | `live` |

#### Lambda IAM 권한 (iamRoleStatements)

- `dynamodb:GetItem/PutItem/Query` → `gurumi-ai-bot-*` 테이블
- `bedrock:InvokeAgent` → `agent-alias/*`

#### RAG 파이프라인

```
S3 documents/ → BedrockDataSource (고정 크기 청킹: 300 토큰, 20% 오버랩)
  → Titan Embeddings V2 (1024차원) → S3VectorIndex
  → Bedrock Agent가 자동으로 Knowledge Base 쿼리
```

#### Outputs

- `KnowledgeBaseId` - Bedrock Knowledge Base ID
- `DataSourceId` - Bedrock Data Source ID
- `AgentId` - Bedrock Agent ID
- `AgentAliasId` - Bedrock Agent Alias ID

### .github/workflows/ - CI/CD

#### push.yml - 인프라 배포

`main` 브랜치 푸시 시 자동 실행:
1. Python 3.12 + 의존성 설치
2. GitHub Variables(비민감) / Secrets(민감)에서 `.env` 생성
3. AWS OIDC 인증 (역할: `lambda-gurumi-ai-bot`)
4. `serverless deploy` (Lambda, DynamoDB, S3, S3 Vectors, KB, Agent 전체 배포)

#### sync-notion.yml - Notion 문서 동기화

매일 UTC 00:00 스케줄 + 수동 실행(`workflow_dispatch`):
1. Notion 페이지를 Markdown으로 내보내기 (`notion-exporter`, 공식 Notion API)
2. S3 `documents/{page_name}/` 프리픽스로 동기화 (`aws s3 sync --delete`)
3. Knowledge Base Ingestion 실행 (문서 → 임베딩 → S3 Vectors)
4. KB/DS ID는 CloudFormation Output에서 자동 조회
5. GitHub Secrets: `NOTION_TOKEN` (Notion Integration API 키)
6. 활성화: GitHub Variables `ENABLE_SYNC_NOTION=true`

#### sync-awsdocs.yml - AWS 공식 문서 동기화

매일 UTC 01:00 스케줄 + 수동 실행(`workflow_dispatch`):
1. `scripts/awsdocs/docs.txt`에 정의된 AWS 공식 PDF 다운로드 (19개 서비스)
2. 50MB 초과 PDF는 `qpdf`로 100페이지 단위 자동 분할
3. S3 `documents/{service}/` 프리픽스로 동기화
4. Knowledge Base Ingestion 실행
5. 활성화: GitHub Variables `ENABLE_SYNC_AWSDOCS=true`
6. 문서 추가/제거: `scripts/awsdocs/docs.txt` 편집

### .github/aws-role/role-policy.json - 배포 IAM 정책

배포 역할의 최소 권한 정책. 서비스별 필요 액션만 포함:
- CloudFormation, Lambda, IAM, S3, DynamoDB, API Gateway, CloudWatch Logs
- S3 Vectors, Bedrock (Knowledge Base, Data Source, Agent)

## 환경 변수

| 변수명 | 기본값 | 용도 |
|--------|--------|------|
| `SLACK_BOT_TOKEN` | (필수) | Slack Bot OAuth 토큰 |
| `SLACK_SIGNING_SECRET` | (필수) | 요청 서명 검증 |
| `AGENT_ID` | (CF 자동) | Bedrock Agent ID (CloudFormation `Fn::GetAtt` 참조) |
| `AGENT_ALIAS_ID` | (CF 자동) | Bedrock Agent Alias ID (CloudFormation `Fn::GetAtt` 참조) |
| `DYNAMODB_TABLE_NAME` | `gurumi-ai-bot-dev` | DynamoDB 테이블명 |
| `AWS_REGION` | `us-east-1` | AWS 리전 |
| `ALLOWED_CHANNEL_IDS` | `None` | 허용 채널 (쉼표 구분, None=전체 허용) |
| `ALLOWED_CHANNEL_MESSAGE` | 영문 메시지 | 비허용 채널 응답 메시지 |
| `PERSONAL_MESSAGE` | `You are a friendly and professional AI assistant.` | 페르소나 프롬프트 |
| `SYSTEM_MESSAGE` | `None` | 시스템 지시사항 |
| `MAX_LEN_SLACK` | `2000` | Slack 메시지 분할 길이 |
| `MAX_LEN_BEDROCK` | `4000` | Bedrock 컨텍스트 최대 길이 |
| `MAX_THROTTLE_COUNT` | `100` | 사용자별 동시 활성 컨텍스트 수 제한 |
| `SLACK_SAY_INTERVAL` | `0` | 분할 메시지 전송 간격 (초) |
| `BOT_CURSOR` | `:robot_face:` | 로딩 표시 이모지 |
| `REACTION_EMOJIS` | `refund-done` | 허용 이모지 리액션 (쉼표 구분) |

## 코드 수정 시 주의사항

- `handler.py`는 단일 파일 구조. 800줄 이상 시 클래스 단위 분리 검토
- `serverless.yml`의 리소스 이름 패턴(`gurumi-ai-bot-*`, `lambda-gurumi-ai-bot-*`)은 `role-policy.json`의 IAM 리소스 패턴과 일치해야 함
- Bedrock Agent는 CloudFormation으로 관리 (`AWS::Bedrock::Agent`). `AGENT_ID`/`AGENT_ALIAS_ID`는 `Fn::GetAtt` 참조
- Knowledge Base는 Agent에 연결되므로 `handler.py`에서 직접 Retrieve API를 호출하지 않음
- DynamoDB TTL은 `expire_at` (Unix timestamp) 속성 사용, 1시간 기본
