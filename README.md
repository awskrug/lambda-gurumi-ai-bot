# lambda-gurumi-ai-bot

A serverless chatbot using AWS Lambda, API Gateway, DynamoDB, and Amazon Bedrock AI models for Slack and Kakao.

![Gurumi Bot](images/gurumi-bot.png)

## Key Features

- Serverless architecture with AWS Lambda and API Gateway
- Conversation history management with DynamoDB
- AI capabilities powered by Amazon Bedrock (Claude models)
- Slack integration with message threading support
- Kakao bot integration
- Channel-based access control
- User throttling to prevent abuse
- Response streaming for better user experience

## Install

```bash
$ brew install python@3.9

$ npm install -g serverless@3.38.0

$ sls plugin install -n serverless-python-requirements
$ sls plugin install -n serverless-dotenv-plugin

$ python -m pip install --upgrade -r requirements.txt
```

## Setup

### Slack Setup

Setup a Slack app by following the guide at https://slack.dev/bolt-js/tutorial/getting-started

Set scopes to Bot Token Scopes in OAuth & Permission:

```
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

Set scopes in Event Subscriptions - Subscribe to bot events

```
app_mention
message.im
```

### Configuration

```bash
$ cp .env.example .env
```

Required environment variables:

```bash
# Required Slack Configuration
SLACK_BOT_TOKEN="xoxb-xxxx"
SLACK_SIGNING_SECRET="xxxx"

# Required for Bedrock Integration
AWS_REGION="us-east-1"
AGENT_ID="xxxxx"
AGENT_ALIAS_ID="xxxxx"
DYNAMODB_TABLE_NAME="gurumi-ai-bot-context"

# Optional Configuration
# KAKAO_BOT_TOKEN="xxxx"
# ALLOWED_CHANNEL_IDS="C12345,C67890"
# ALLOWED_CHANNEL_MESSAGE="Sorry, I'm not allowed to respond in this channel."
# PERSONAL_MESSAGE="You are a friendly and professional AI assistant."
# SYSTEM_MESSAGE="Additional system prompt instructions"
# MAX_LEN_SLACK=2000
# MAX_LEN_BEDROCK=4000
# MAX_THROTTLE_COUNT=100
# SLACK_SAY_INTERVAL=0
# BOT_CURSOR=":robot_face:"
```

## Deployment

Deploy to AWS:

```bash
$ sls deploy --region us-east-1
```

## Testing

### Slack URL Verification Test

```bash
curl -X POST -H "Content-Type: application/json" \
-d " \
{ \
    \"token\": \"Jhj5dZrVaK7ZwHHjRyZWjbDl\", \
    \"challenge\": \"3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P\", \
    \"type\": \"url_verification\" \
}" \
https://xxxx.execute-api.us-east-1.amazonaws.com/dev/slack/events
```

### Kakao Bot Test

```bash
curl -X POST -H "Content-Type: application/json" \
-H "Authorization: Bearer YOUR_KAKAO_BOT_TOKEN" \
-d " \
{ \
    \"query\": \"Hello, how are you?\" \
}" \
https://xxxx.execute-api.us-east-1.amazonaws.com/dev/kakao/chat
```

## Architecture

For a detailed architectural overview of this application, see [architecture.md](architecture.md).

## References

* [AWS Bedrock Documentation](https://docs.aws.amazon.com/bedrock/)
* [Slack Bolt Framework](https://slack.dev/bolt-js/)
* [Serverless Framework](https://www.serverless.com/)
