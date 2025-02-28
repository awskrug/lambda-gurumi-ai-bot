# Lambda Gurumi AI Bot - Architecture

This document outlines the architecture of the Lambda Gurumi AI Bot, a serverless application that integrates Slack with AWS Bedrock's AI models to provide conversational AI capabilities.

## System Overview

The Lambda Gurumi AI Bot is a serverless application built on AWS that connects Slack with AWS Bedrock's AI models. It allows users to interact with AI through Slack by mentioning the bot in channels or sending direct messages. The bot leverages AWS Bedrock's Claude and Stable Diffusion models for natural language processing and image generation.

![Gurumi Bot](images/gurumi-bot.png)

## Key Components

### 1. AWS Services

- **AWS Lambda**: Executes the serverless function that processes Slack events and communicates with AWS Bedrock
- **API Gateway**: Provides HTTP endpoints for Slack to send events to the Lambda function
- **DynamoDB**: Stores conversation context and manages user throttling
- **S3**: Stores data and assets
- **AWS Bedrock**: Provides AI capabilities through various foundation models
  - Claude models for text generation
  - Stable Diffusion for image generation
  - Knowledge Base integration for enhanced responses
  - Agent functionality for complex task handling

### 2. External Services

- **Slack API**: Receives and sends messages through the Slack platform
  - Handles mentions and direct messages
  - Manages threaded conversations
  - Supports file uploads

### 3. Application Components

- **Slack Bolt Framework**: Handles Slack event processing and message formatting
- **Bedrock Integration Modules**:
  - `invoke_agent.py`: Handles AWS Bedrock Agent interactions
  - `invoke_claude_3.py`: Manages Claude 3 model interactions
  - `invoke_claude_3_image.py`: Handles image analysis with Claude 3
  - `invoke_stable_diffusion.py`: Manages image generation with Stable Diffusion
  - `invoke_knowledge_base.py`: Integrates with Bedrock Knowledge Base
  - `converse_stream.py`: Handles streaming conversations
- **Utility Scripts** (in bin/):
  - Notion integration tools
  - Data processing utilities

## Data Flow

1. **Event Reception**:
   - Slack sends events (mentions or direct messages) to the API Gateway endpoint
   - API Gateway forwards these events to the Lambda function

2. **Event Processing**:
   - Lambda function validates the Slack event
   - Checks channel permissions (if configured)
   - For new conversations, stores a record in DynamoDB to prevent duplicate processing
   - Manages user throttling through DynamoDB context counting

3. **Conversation Handling**:
   - Retrieves conversation history from threads (if applicable)
   - Formats prompts with system messages and conversation context
   - Streams responses back to Slack in chunks for better user experience
   - Handles message splitting for long responses

4. **AI Integration**:
   - Communicates with AWS Bedrock services for:
     - Text generation using Claude models
     - Image analysis using Claude 3
     - Image generation using Stable Diffusion
     - Knowledge base queries
     - Agent-based task handling

## Technical Details

### Serverless Configuration

The application is deployed using the Serverless Framework with the following configuration:
- Runtime: Python 3.9
- Timeout: 600 seconds (10 minutes)
- Memory: Configurable (commented out in serverless.yml)
- Region: us-east-1 (default)

### IAM Permissions

The Lambda function has permissions for:
- DynamoDB operations
- Bedrock Agent invocation
- Bedrock Knowledge Base retrieval
- Bedrock model invocation (Claude and Stable Diffusion)
- S3 bucket access

### DynamoDB Schema

- **Table Name**: gurumi-ai-bot-context
- **Primary Key**: id (String)
- **TTL Field**: expire_at (1 hour)
- **Attributes**:
  - id: Thread ID or user ID
  - user: User identifier
  - conversation: Conversation content
  - expire_dt: Human-readable expiration datetime
  - expire_at: Unix timestamp for TTL

### Environment Variables

Key configuration options include:
- Slack credentials
- AWS region settings
- Bedrock agent and model configurations
- Channel restrictions
- System messages and personal messages
- Message length limits
- Throttling controls

### Security Features

- AWS IAM roles with least privilege access
- Environment variables for sensitive credentials
- Channel-based access control
- User request throttling
- DynamoDB TTL for automatic cleanup
- Duplicate message prevention

## Deployment Process

1. Install dependencies:
   - Python 3.9
   - Serverless Framework
   - Required plugins and packages

2. Configure environment:
   - Copy and configure .env file
   - Set up Slack app permissions
   - Configure AWS Bedrock settings

3. Deploy using Serverless Framework:
   ```bash
   sls deploy --region us-east-1
   ```

## Extension Points

The architecture supports several enhancement possibilities:
- Adding new Bedrock model integrations
- Expanding Knowledge Base capabilities
- Implementing additional Slack event handlers
- Enhanced monitoring and analytics
- Additional utility scripts for data processing
- Integration with other AWS services
