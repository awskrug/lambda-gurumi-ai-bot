import boto3
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Union

from slack_bolt import App, Say
from slack_bolt.adapter.aws_lambda import SlackRequestHandler

from boto3.dynamodb.conditions import Key


# Environment configuration
class Config:
    """Configuration settings loaded from environment variables"""
    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
    SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
    DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "gurumi-ai-bot-dev")
    KAKAO_BOT_TOKEN = os.environ.get("KAKAO_BOT_TOKEN", "None")
    AGENT_ID = os.environ.get("AGENT_ID", "None")
    AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID", "None")
    ALLOWED_CHANNEL_IDS = os.environ.get("ALLOWED_CHANNEL_IDS", "None")
    ALLOWED_CHANNEL_MESSAGE = os.environ.get(
        "ALLOWED_CHANNEL_MESSAGE", "Sorry, I'm not allowed to respond in this channel."
    )
    PERSONAL_MESSAGE = os.environ.get(
        "PERSONAL_MESSAGE", "You are a friendly and professional AI assistant."
    )
    SYSTEM_MESSAGE = os.environ.get("SYSTEM_MESSAGE", "None")
    MAX_LEN_SLACK = int(os.environ.get("MAX_LEN_SLACK", "2000"))
    MAX_LEN_BEDROCK = int(os.environ.get("MAX_LEN_BEDROCK", "4000"))
    MAX_THROTTLE_COUNT = int(os.environ.get("MAX_THROTTLE_COUNT", "100"))
    SLACK_SAY_INTERVAL = float(os.environ.get("SLACK_SAY_INTERVAL", "0"))
    BOT_CURSOR = os.environ.get("BOT_CURSOR", ":robot_face:")
    REACTION_EMOJIS = os.environ.get("REACTION_EMOJIS", "refund-done")

    @classmethod
    def get_reaction_emojis(cls) -> List[str]:
        """Parse comma-separated reaction emojis into a list"""
        if not cls.REACTION_EMOJIS or cls.REACTION_EMOJIS == "None":
            return []
        return [emoji.strip() for emoji in cls.REACTION_EMOJIS.split(",") if emoji.strip()]

    @classmethod
    def validate(cls) -> bool:
        """Validate required configuration settings"""
        required_vars = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"]
        missing = [var for var in required_vars if not getattr(cls, var)]
        if missing:
            print(f"Missing required environment variables: {', '.join(missing)}")
            return False
        return True


# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(Config.DYNAMODB_TABLE_NAME)
bedrock_agent_client = boto3.client("bedrock-agent-runtime", region_name=Config.AWS_REGION)

# Initialize Slack app
app = App(
    token=Config.SLACK_BOT_TOKEN,
    signing_secret=Config.SLACK_SIGNING_SECRET,
    process_before_response=True,
)

# Lazy initialization for bot_id to avoid API call at module load time
_bot_id: Optional[str] = None


def get_bot_id() -> str:
    """Get Slack bot ID with lazy initialization"""
    global _bot_id
    if _bot_id is None:
        _bot_id = app.client.api_call("auth.test")["user_id"]
    return _bot_id

# Status messages
MSG_PREVIOUS = f"이전 대화 내용 확인 중... {Config.BOT_CURSOR}"
MSG_RESPONSE = f"응답 기다리는 중... {Config.BOT_CURSOR}"
MSG_ERROR = f"오류가 발생했습니다. 잠시 후 다시 시도해주세요. {Config.BOT_CURSOR}"


class DynamoDBManager:
    """Handles DynamoDB operations for conversation context"""

    @staticmethod
    def get_context(thread_ts: Optional[str], user: str, default: str = "") -> str:
        """Retrieve conversation context from DynamoDB"""
        try:
            key = {"id": thread_ts or user}
            item = table.get_item(Key=key).get("Item")
            return item["conversation"] if item else default
        except Exception as e:
            print(f"Error retrieving context: {e}")
            return default

    @staticmethod
    def put_context(thread_ts: Optional[str], user: str, conversation: str = "") -> None:
        """Store conversation context in DynamoDB with TTL"""
        try:
            expire_at = int(time.time()) + 3600  # 1 hour TTL
            expire_dt = datetime.fromtimestamp(expire_at).isoformat()

            item = {
                "id": thread_ts or user,
                "conversation": conversation,
                "expire_dt": expire_dt,
                "expire_at": expire_at,
            }

            if thread_ts:
                item["user"] = user

            table.put_item(Item=item)
        except Exception as e:
            print(f"Error storing context: {e}")

    @staticmethod
    def count_user_contexts(user: str) -> int:
        """Count contexts belonging to a specific user using GSI"""
        try:
            response = table.query(
                IndexName="user-index",
                KeyConditionExpression=Key("user").eq(user),
                Select="COUNT"
            )
            return response.get("Count", 0)
        except Exception as e:
            print(f"Error counting contexts: {e}")
            return 0


class MessageFormatter:
    """Handles message formatting and splitting for Slack"""

    @staticmethod
    def split_message(message: str, max_len: int) -> List[str]:
        """Split a message into chunks that fit within max_len"""
        # If message is empty or smaller than max_len, return as is
        if not message or len(message) <= max_len:
            return [message]

        # First split by code blocks
        parts = []
        segments = message.split("```")

        for i, segment in enumerate(segments):
            if not segment:  # Skip empty segments
                continue

            if i % 2 == 1:  # This is a code block
                # Preserve the code block formatting
                code_parts = MessageFormatter._split_text(f"```{segment}```", max_len)
                parts.extend(code_parts)
            else:
                # Regular text - split by paragraphs
                text_parts = MessageFormatter._split_text(segment, max_len)
                parts.extend(text_parts)

        # Final cleanup to ensure no part exceeds max_len
        result = []
        current = ""

        for part in parts:
            if len(current) + len(part) + 2 <= max_len:
                if current:
                    current += "\n\n" + part
                else:
                    current = part
            else:
                if current:
                    result.append(current)
                current = part

        if current:
            result.append(current)

        return result

    @staticmethod
    def _split_text(text: str, max_len: int) -> List[str]:
        """Helper method to split text by paragraphs"""
        if len(text) <= max_len:
            return [text]

        parts = text.split("\n\n")
        result = []
        current = ""

        for part in parts:
            # If a single part is longer than max_len, split it by sentences
            if len(part) > max_len:
                sentences = re.split(r'(?<=[.!?])\s+', part)
                for sentence in sentences:
                    if len(current) + len(sentence) + 2 <= max_len:
                        if current:
                            current += " " + sentence
                        else:
                            current = sentence
                    else:
                        if current:
                            result.append(current)
                        current = sentence
            elif len(current) + len(part) + 2 <= max_len:
                if current:
                    current += "\n\n" + part
                else:
                    current = part
            else:
                if current:
                    result.append(current)
                current = part

        if current:
            result.append(current)

        return result


class SlackManager:
    """Handles Slack messaging operations"""

    # Cache for user display names to avoid repeated API calls
    _user_name_cache: Dict[str, str] = {}

    @classmethod
    def get_user_display_name(cls, user_id: str) -> str:
        """Get user display name from Slack API with caching"""
        if user_id in cls._user_name_cache:
            return cls._user_name_cache[user_id]

        try:
            response = app.client.users_info(user=user_id)
            if response.get("ok"):
                user_info = response.get("user", {})
                profile = user_info.get("profile", {})
                # Prefer display_name, fall back to real_name, then user_id
                display_name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or user_id
                )
                cls._user_name_cache[user_id] = display_name
                return display_name
        except Exception as e:
            print(f"Error fetching user info for {user_id}: {e}")

        return user_id

    @staticmethod
    def update_message(say: Say, channel: str, thread_ts: Optional[str],
                      latest_ts: str, message: str) -> tuple:
        """Update existing message and send additional messages if needed"""
        try:
            split_messages = MessageFormatter.split_message(message, Config.MAX_LEN_SLACK)

            for i, text in enumerate(split_messages):
                if i == 0:
                    # Update the initial message
                    app.client.chat_update(channel=channel, ts=latest_ts, text=text)
                else:
                    # Add delay if configured
                    if Config.SLACK_SAY_INTERVAL > 0:
                        time.sleep(Config.SLACK_SAY_INTERVAL)

                    # Send additional messages in thread
                    result = say(text=text, thread_ts=thread_ts)
                    latest_ts = result["ts"]

            return message, latest_ts
        except Exception as e:
            print(f"Error updating message: {e}")
            # Update with error message
            app.client.chat_update(channel=channel, ts=latest_ts, text=MSG_ERROR)
            return MSG_ERROR, latest_ts

    @classmethod
    def get_thread_history(cls, channel: str, thread_ts: str, client_msg_id: str) -> List[str]:
        """Retrieve conversation history from a Slack thread"""
        contexts = []

        try:
            response = app.client.conversations_replies(channel=channel, ts=thread_ts)

            if not response.get("ok"):
                print("Failed to retrieve thread messages")
                return contexts

            messages = response.get("messages", [])

            # Slack API returns messages in chronological order (oldest first)
            # First message is the thread parent, skip it by slicing from index 1
            # Process from newest to oldest to prioritize recent context
            thread_messages = messages[1:]  # Exclude thread parent
            thread_messages.reverse()  # Now newest first

            for message in thread_messages:
                # Skip the current message being processed
                if message.get("client_msg_id") == client_msg_id:
                    continue

                # Determine role and author name
                if message.get("bot_id"):
                    role = "assistant"
                    author = "assistant"
                else:
                    role = "user"
                    user_id = message.get("user", "")
                    author = cls.get_user_display_name(user_id) if user_id else "user"

                contexts.append(f"{role}({author}): {message.get('text', '')}")

                # Check if we've reached the context length limit
                context_text = "\n".join(contexts)
                if len(context_text) > Config.MAX_LEN_BEDROCK:
                    contexts.pop(0)  # Remove oldest (first added) message
                    break

            # Reverse back to chronological order for the prompt
            contexts.reverse()

        except Exception as e:
            print(f"Error retrieving thread history: {e}")

        return contexts


class BedrockManager:
    """Handles Amazon Bedrock operations"""

    @staticmethod
    def invoke_agent(prompt: str) -> str:
        """Invoke Amazon Bedrock Agent with prompt and return response"""
        try:
            # Create a unique session ID
            now = datetime.now()
            session_id = str(int(now.timestamp() * 1000))

            # Call Bedrock Agent
            response = bedrock_agent_client.invoke_agent(
                agentId=Config.AGENT_ID,
                agentAliasId=Config.AGENT_ALIAS_ID,
                sessionId=session_id,
                inputText=prompt,
            )

            # Process streaming response
            completion = ""
            for event in response.get("completion"):
                chunk = event["chunk"]
                completion += chunk["bytes"].decode()

            return completion

        except Exception as e:
            print(f"Error invoking Bedrock Agent: {e}")
            return f"죄송합니다. 응답을 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요. (오류: {type(e).__name__})"

    @staticmethod
    def create_prompt(say: Optional[Say], query: str, thread_ts: Optional[str] = None,
                    channel: Optional[str] = None, client_msg_id: Optional[str] = None,
                    latest_ts: Optional[str] = None) -> str:
        """Create a prompt for the AI model with context and query"""
        prompts = []
        prompts.append(f"User: {Config.PERSONAL_MESSAGE}")

        if Config.SYSTEM_MESSAGE != "None":
            prompts.append(Config.SYSTEM_MESSAGE)

        prompts.append("<question> 태그로 감싸진 질문에 답변을 제공하세요.")

        try:
            # Add conversation history if in a thread
            if thread_ts and say and channel and client_msg_id and latest_ts:
                # Update status message
                SlackManager.update_message(say, channel, thread_ts, latest_ts, MSG_PREVIOUS)

                # Get thread history
                contexts = SlackManager.get_thread_history(channel, thread_ts, client_msg_id)

                if contexts:
                    prompts.append("<history> 에 정보가 제공 되면, 대화 기록을 참고하여 답변해 주세요.")
                    prompts.append("<history>")
                    prompts.append("\n\n".join(contexts))
                    prompts.append("</history>")

            # Add the current query
            prompts.append("")
            prompts.append("<question>")
            prompts.append(query)
            prompts.append("</question>")
            prompts.append("")

            prompts.append("Assistant:")

            return "\n".join(prompts)

        except Exception as e:
            print(f"Error creating prompt: {e}")
            raise e


def conversation(say: Say, query: str, thread_ts: Optional[str] = None,
               channel: Optional[str] = None, client_msg_id: Optional[str] = None) -> None:
    """Main conversation handler that processes queries and returns AI responses"""
    print(f"conversation: query: {query}")

    try:
        # Send initial status message
        result = say(text=Config.BOT_CURSOR, thread_ts=thread_ts)
        latest_ts = result["ts"]

        # Create prompt with context and query
        prompt = BedrockManager.create_prompt(
            say, query, thread_ts, channel, client_msg_id, latest_ts
        )

        # Update status while waiting for response
        SlackManager.update_message(say, channel, thread_ts, latest_ts, MSG_RESPONSE)

        # Get response from AI
        message = BedrockManager.invoke_agent(prompt)

        # Send final response
        SlackManager.update_message(say, channel, thread_ts, latest_ts, message)

    except Exception as e:
        print(f"Error in conversation handler: {e}")
        # Update with error message if possible
        try:
            if latest_ts:
                SlackManager.update_message(say, channel, thread_ts, latest_ts, MSG_ERROR)
        except Exception:
            pass


@app.event("app_mention")
def handle_mention(body: Dict[str, Any], say: Say) -> None:
    """Handle mentions of the bot in channels"""
    print(f"handle_mention: {body}")

    event = body["event"]
    thread_ts = event.get("thread_ts", event.get("ts"))
    channel = event.get("channel")
    client_msg_id = event.get("client_msg_id")

    # Check if the channel is allowed
    if Config.ALLOWED_CHANNEL_IDS != "None":
        allowed_channel_ids = Config.ALLOWED_CHANNEL_IDS.split(",")
        if channel not in allowed_channel_ids:
            first_channel = f"<#{allowed_channel_ids[0]}>"
            message = Config.ALLOWED_CHANNEL_MESSAGE.format(first_channel)
            say(text=message, thread_ts=thread_ts)
            print(f"handle_mention: {message}")
            return

    # Extract query text (remove the bot mention)
    prompt = re.sub(f"<@{get_bot_id()}>", "", event["text"]).strip()

    # Process the conversation
    conversation(say, prompt, thread_ts, channel, client_msg_id)


@app.event("message")
def handle_message(body: Dict[str, Any], say: Say) -> None:
    """Handle direct messages to the bot"""
    print(f"handle_message: {body}")

    event = body["event"]

    # Ignore messages from bots (including this bot)
    if event.get("bot_id"):
        return

    channel = event["channel"]
    client_msg_id = event["client_msg_id"]
    prompt = event["text"].strip()

    # Process the conversation (thread_ts=None for DMs)
    conversation(say, prompt, None, channel, client_msg_id)


def mask_account_number(account: str) -> str:
    """Mask account number, showing only first 4 and last 2 digits"""
    # Remove any non-digit characters for processing
    digits_only = re.sub(r'\D', '', account)

    if len(digits_only) <= 6:
        # If too short, mask middle portion
        return digits_only[:2] + '*' * (len(digits_only) - 2)

    # Show first 4 and last 2 digits
    return digits_only[:4] + '*' * (len(digits_only) - 6) + digits_only[-2:]


def process_refund_done(channel: str, message_ts: str, user: str) -> None:
    """Process refund-done emoji reaction: mask account number and add refund timestamp"""
    print(f"process_refund_done: channel={channel}, message_ts={message_ts}, user={user}")

    try:
        # Get the original message
        result = app.client.conversations_history(
            channel=channel,
            latest=message_ts,
            limit=1,
            inclusive=True
        )

        if not result.get("ok") or not result.get("messages"):
            print("Failed to retrieve message")
            return

        message = result["messages"][0]
        blocks = message.get("blocks", [])

        if not blocks:
            print("No blocks found in message")
            return

        # Check if this is a refund request message (has the header)
        is_refund_message = False
        for block in blocks:
            if block.get("type") == "header":
                header_text = block.get("text", {}).get("text", "")
                if "환불 신청" in header_text:
                    is_refund_message = True
                    break

        if not is_refund_message:
            print("Not a refund request message")
            return

        # Process blocks to mask account number and add refund timestamp
        updated_blocks = []
        refund_time_added = False
        # Convert to KST (UTC+9)
        kst = timezone(timedelta(hours=9))
        current_time = datetime.now(kst).strftime("%Y. %m. %d. %p %I:%M:%S").replace("AM", "오전").replace("PM", "오후")

        for block in blocks:
            if block.get("type") == "section" and block.get("fields"):
                new_fields = []
                for field in block["fields"]:
                    field_text = field.get("text", "")

                    # Mask account number
                    if "*계좌번호:*" in field_text:
                        lines = field_text.split("\n")
                        if len(lines) >= 2:
                            account = lines[1]
                            masked = mask_account_number(account)
                            field = {
                                "type": "mrkdwn",
                                "text": f"*계좌번호:*\n{masked}"
                            }

                    new_fields.append(field)

                # Add refund timestamp if not already present
                has_refund_time = any("*환불일시:*" in f.get("text", "") for f in new_fields)
                if not has_refund_time and not refund_time_added:
                    new_fields.append({
                        "type": "mrkdwn",
                        "text": f"*환불일시:*\n{current_time}"
                    })
                    refund_time_added = True

                block = dict(block)
                block["fields"] = new_fields

            updated_blocks.append(block)

        # Update the message
        app.client.chat_update(
            channel=channel,
            ts=message_ts,
            blocks=updated_blocks,
            text=message.get("text", "환불 신청이 처리되었습니다.")
        )

        print(f"Refund message updated successfully")

    except Exception as e:
        print(f"Error processing refund done: {e}")


# Reaction handlers mapping: emoji name -> handler function
REACTION_HANDLERS = {
    "refund-done": lambda channel, ts, user: process_refund_done(channel, ts, user),
}


@app.event("reaction_added")
def handle_reaction_added(body: Dict[str, Any]) -> None:
    """Handle emoji reaction added events"""
    print(f"handle_reaction_added: {body}")

    event = body["event"]
    reaction = event.get("reaction", "")
    user = event.get("user", "")
    item = event.get("item", {})

    # Check if this reaction is in the allowed list
    allowed_emojis = Config.get_reaction_emojis()
    if reaction not in allowed_emojis:
        return

    # Get message details
    item_type = item.get("type", "")
    if item_type != "message":
        return

    channel = item.get("channel", "")
    message_ts = item.get("ts", "")

    if not channel or not message_ts:
        return

    # Dispatch to the appropriate handler
    handler = REACTION_HANDLERS.get(reaction)
    if handler:
        handler(channel, message_ts, user)
    else:
        print(f"No handler found for reaction: {reaction}")


def success(message: str = "") -> Dict[str, Any]:
    """Return a success response for Lambda"""
    return {
        "statusCode": 200,
        "headers": {"Content-type": "application/json"},
        "body": json.dumps({"status": "Success", "message": message}),
    }


def unauthorized() -> Dict[str, Any]:
    """Return an unauthorized response for Lambda"""
    return {
        "statusCode": 401,
        "headers": {"Content-type": "application/json"},
        "body": json.dumps({"status": "Error", "message": "Unauthorized"}),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Main Lambda handler for Slack events"""
    # Validate required configuration
    if not Config.validate():
        return {
            "statusCode": 500,
            "headers": {"Content-type": "application/json"},
            "body": json.dumps({"status": "Error", "message": "Missing required configuration"}),
        }

    # Parse request body
    body = json.loads(event["body"])

    # Handle Slack verification challenge
    if "challenge" in body:
        return {
            "statusCode": 200,
            "headers": {"Content-type": "application/json"},
            "body": json.dumps({"challenge": body["challenge"]}),
        }

    print(f"lambda_handler: {body}")

    # Check for valid event structure
    if "event" not in body:
        print("lambda_handler: event not found")
        return success()

    event_type = body["event"].get("type", "")

    # Handle reaction events directly (no client_msg_id, no deduplication needed)
    if event_type == "reaction_added":
        slack_handler = SlackRequestHandler(app=app)
        return slack_handler.handle(event, context)

    # For message events, check client_msg_id
    if "client_msg_id" not in body["event"]:
        print("lambda_handler: client_msg_id not found")
        return success()

    # Extract message identifiers
    token = body["event"]["client_msg_id"]
    user = body["event"]["user"]

    # Check for duplicate events (idempotency)
    if DynamoDBManager.get_context(token, user) != "":
        print("lambda_handler: duplicate event detected")
        return success()

    # Check user throttling
    count = DynamoDBManager.count_user_contexts(user)
    if count >= Config.MAX_THROTTLE_COUNT:
        print(f"lambda_handler: throttle limit reached: {count} >= {Config.MAX_THROTTLE_COUNT}")
        return success()

    # Store context to prevent duplicate processing
    DynamoDBManager.put_context(token, user, body["event"]["text"])

    # Handle the Slack event
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)


def kakao_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle Kakao bot events"""
    print(f"kakao_handler: {event}")

    # Validate authentication
    headers = event.get("headers", {})
    auth_header = headers.get("Authorization", "")

    # Check for Authorization header and validate token
    if not auth_header or auth_header != f"Bearer {Config.KAKAO_BOT_TOKEN}":
        print("kakao_handler: unauthorized request")
        return unauthorized()

    # Parse request body
    try:
        body = json.loads(event["body"])
    except Exception as e:
        print(f"kakao_handler: error parsing body: {e}")
        return success()

    # Check if query exists
    if "query" not in body:
        print("kakao_handler: no query found")
        return success()

    query = body["query"]
    print(f"kakao_handler: query: {query}")

    # Create prompt and get response
    try:
        prompt = BedrockManager.create_prompt(None, query)
        message = BedrockManager.invoke_agent(prompt)
        return success(message)
    except Exception as e:
        print(f"kakao_handler: error processing query: {e}")
        return success("죄송합니다. 응답을 생성하는 중 오류가 발생했습니다.")
