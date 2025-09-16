# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A serverless chatbot application that integrates with Slack and Kakao, powered by AWS Lambda, Amazon Bedrock AI models, and DynamoDB for conversation persistence.

## Common Commands

### Initial Setup
```bash
# Install Python 3.12 if not available
brew install python@3.12

# Install Serverless Framework with specific version
npm install -g serverless@3.38.0

# Install project dependencies
npm install
sls plugin install -n serverless-python-requirements
sls plugin install -n serverless-dotenv-plugin
python -m pip install --upgrade -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env with required credentials and settings
```

### Deployment
```bash
# Deploy to AWS (default stage: dev)
sls deploy --region us-east-1

# Deploy to specific stage
sls deploy --stage prod --region us-east-1

# Remove deployment
sls remove --region us-east-1
```

### Testing Bedrock Integration
```bash
# Test scripts in bin/bedrock/
cd bin/bedrock
python invoke_agent.py -p "Your prompt here"
python invoke_claude_3.py -p "Your prompt here"
python invoke_stable_diffusion.py -p "Image generation prompt"
```

### Local Testing
```bash
# Test Slack URL verification
curl -X POST -H "Content-Type: application/json" \
  -d '{"token": "test", "challenge": "test_challenge", "type": "url_verification"}' \
  https://your-api-url/dev/slack/events

# Test Kakao bot endpoint
curl -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_KAKAO_BOT_TOKEN" \
  -d '{"query": "Hello"}' \
  https://your-api-url/dev/kakao/chat
```

## Architecture

### Core Components

1. **handler.py** - Main Lambda function entry points
   - `lambda_handler`: Processes Slack events (app mentions, direct messages)
   - `kakao_handler`: Processes Kakao bot requests
   - Contains all business logic including conversation management, throttling, and AI integration

2. **Key Classes**:
   - `Config`: Centralized environment configuration management
   - `DynamoDBManager`: Handles conversation context storage with TTL
   - `ThrottleManager`: User rate limiting to prevent abuse
   - `SlackMessage`: Message formatting and threading utilities
   - `BedrockAgent`: AI model integration (Claude 3, agents, image generation)

3. **AWS Resources** (defined in serverless.yml):
   - Lambda Functions: `mention` (Slack) and `kakao` handlers
   - DynamoDB Table: Conversation history with TTL-based expiration
   - S3 Bucket: File storage for bot operations
   - IAM Permissions: DynamoDB access, Bedrock model invocation

### Data Flow

1. **Slack Integration**:
   - Receives events via HTTP POST to `/slack/events`
   - Validates requests using signing secret
   - Manages threaded conversations and maintains context
   - Streams responses in chunks to avoid timeouts

2. **Conversation Context**:
   - Stored in DynamoDB with thread_ts or user_id as key
   - 1-hour TTL for automatic cleanup
   - Context includes full conversation history for AI continuity

3. **AI Processing**:
   - Routes to appropriate Bedrock model based on request type
   - Supports Claude 3 for text, Stable Diffusion for images
   - Can invoke custom agents with knowledge bases
   - Implements streaming for long responses

### Environment Variables

Critical configuration managed through `.env` file:
- `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` - Slack authentication
- `AGENT_ID`, `AGENT_ALIAS_ID` - Bedrock agent configuration
- `DYNAMODB_TABLE_NAME` - Context storage table
- `ALLOWED_CHANNEL_IDS` - Channel access control
- `MAX_THROTTLE_COUNT` - Rate limiting threshold
- `MAX_LEN_SLACK`, `MAX_LEN_BEDROCK` - Message length limits

### Deployment Pipeline

GitHub Actions workflow (`.github/workflows/push.yml`):
1. Triggers on push to main branch
2. Sets up Python 3.12 environment
3. Installs all dependencies
4. Configures environment variables from GitHub secrets
5. Assumes AWS role for deployment
6. Deploys using Serverless Framework

## Key Implementation Details

- **Message Threading**: Uses Slack's thread_ts to maintain conversation context
- **Rate Limiting**: Per-user throttling with configurable limits to prevent abuse
- **Error Handling**: Graceful degradation with user-friendly error messages
- **Response Streaming**: Breaks long AI responses into chunks to avoid Slack timeouts
- **Channel Filtering**: Optional whitelist of allowed Slack channels
- **Context Persistence**: DynamoDB with automatic TTL-based cleanup
- **Multi-model Support**: Claude for text, Stable Diffusion for images, custom agents for specialized tasks
