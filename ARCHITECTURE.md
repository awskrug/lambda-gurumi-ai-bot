# Lambda Gurumi AI Bot - Architecture

This document outlines the architecture of the Lambda Gurumi AI Bot, a serverless application that integrates Slack and Kakao with AWS Bedrock's AI models to provide conversational AI capabilities.

## System Overview

The Lambda Gurumi AI Bot is a serverless application built on AWS that connects messaging platforms (Slack and Kakao) with AWS Bedrock's AI models. It allows users to interact with AI through Slack channels/direct messages or Kakao bot interface. The bot leverages AWS Bedrock's Claude models for natural language processing.

![Gurumi Bot](images/gurumi-bot.png)

## Core Components

### AWS Services
- **AWS Lambda**: Executes the serverless function that processes Slack/Kakao events and communicates with AWS Bedrock
- **API Gateway**: Provides HTTP endpoints for Slack and Kakao to send events to the Lambda function
- **DynamoDB**: Stores conversation context and manages user throttling
- **Amazon Bedrock**: Provides AI capabilities through Claude models and Agent functionality

### External Integrations
- **Slack API**: Handles mentions and direct messages with threaded conversations
- **Kakao Bot API**: Provides a simple interface for text-based queries

### Application Structure
- **Main Handler (handler.py)**: Core application logic with several key components:
  - **Config**: Environment variable management
  - **DynamoDBManager**: Context storage and user throttling
  - **MessageFormatter**: Message splitting and formatting
  - **SlackManager**: Slack-specific operations
  - **BedrockManager**: Amazon Bedrock interactions
  - **Event Handlers**: Slack and Kakao event processing

## Technical Design

### Config Class
Environment variable management with validation for required settings:
- Slack credentials
- AWS region and service configurations
- Platform tokens and IDs
- Message formatting settings
- Channel access control
- Throttling parameters

### DynamoDBManager Class
Handles conversation persistence and user throttling:
- Stores conversation context with TTL (1 hour)
- Retrieves previous conversations
- Counts contexts per user for throttling
- Prevents duplicate message processing

### MessageFormatter Class
Handles message formatting for Slack:
- Splits long messages to fit Slack limits
- Preserves code blocks during splitting
- Implements intelligent paragraph and sentence splitting

### SlackManager Class
Manages Slack-specific operations:
- Updates existing messages
- Sends additional messages in threads
- Retrieves conversation history
- Handles role identification (user vs. assistant)

### BedrockManager Class
Handles Amazon Bedrock interactions:
- Creates well-formatted prompts with conversation history
- Invokes Amazon Bedrock Agent with session management
- Processes streaming responses
- Handles error conditions

### Event Handlers
Process incoming events from different platforms:
- **Slack Mentions**: `handle_mention` for channel mentions
- **Slack Direct Messages**: `handle_message` for DMs
- **Kakao Requests**: `kakao_handler` for text queries
- **Main Lambda Handler**: `lambda_handler` for event routing with validation and duplicate prevention

## Data Flow

1. **Event Reception**:
   - Slack/Kakao sends events to API Gateway endpoints
   - Lambda function receives and validates events

2. **Pre-processing**:
   - Checks for configuration validity
   - Validates authentication (especially for Kakao)
   - Prevents duplicate event processing
   - Implements user throttling

3. **Conversation Processing**:
   - Extracts query text
   - Retrieves conversation history (for Slack threads)
   - Creates appropriate prompt with context

4. **AI Interaction**:
   - Sends formatted prompt to Bedrock Agent
   - Processes streaming response
   - Handles errors gracefully

5. **Response Delivery**:
   - Formats AI response for the platform
   - Splits long messages if needed
   - Updates initial "waiting" message (for Slack)
   - Returns formatted response

## Security Features

- **Authentication Validation**:
  - Slack signing secret verification
  - Kakao bearer token authentication
  - AWS IAM roles with least privilege

- **User Protection**:
  - Channel-based access control
  - Per-user throttling (configurable limit)
  - Duplicate message prevention

- **Data Management**:
  - Automatic context cleanup via DynamoDB TTL
  - No permanent storage of conversation content
  - Context length limits

## Deployment & Configuration

### Required Configuration
- Slack credentials
- AWS Bedrock Agent ID and Alias ID
- DynamoDB table name
- AWS region

### Optional Configuration
- Kakao Bot token
- Channel restriction IDs
- System and personal messages
- Message length and throttling parameters

### Environment Files
Configuration is managed through environment variables, typically stored in `.env` files with different versions for different environments (dev, staging, prod).

## Extension Points

- **Additional Messaging Platforms**: Structure allows adding new platform integrations
- **Enhanced AI Capabilities**: Supports expanding Bedrock model integrations
- **Custom Prompt Engineering**: Customizable system and personal messages
- **Advanced Context Management**: DynamoDB structure allows for metadata expansion
