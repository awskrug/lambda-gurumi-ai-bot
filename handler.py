import boto3
import datetime
import json
import os
import re
import sys
import time

from slack_bolt import App, Say
from slack_bolt.adapter.aws_lambda import SlackRequestHandler

BOT_CURSOR = os.environ.get("BOT_CURSOR", ":robot_face:")

# Set up Slack API credentials
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# Keep track of conversation history by thread and user
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "gureumi-ai-bot-context")

# Amazon Bedrock Model ID
TEXT_MODEL_ID = os.environ.get("TEXT_MODEL_ID", "anthropic.claude-3")
IMAGE_MODEL_ID = os.environ.get("IMAGE_MODEL_ID", "stability.stable-diffusion-xl")

ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 1024))

# Set up System messages
SYSTEM_MESSAGE = os.environ.get("SYSTEM_MESSAGE", "None")

MESSAGE_MAX = int(os.environ.get("MESSAGE_MAX", 4000))

# Initialize Slack app
app = App(
    token=SLACK_BOT_TOKEN,
    signing_secret=SLACK_SIGNING_SECRET,
    process_before_response=True,
)

bot_id = app.client.api_call("auth.test")["user_id"]

# Initialize DynamoDB
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE_NAME)

# Initialize the Amazon Bedrock runtime client
bedrock = boto3.client(service_name="bedrock-runtime", region_name="us-east-1")


# Get the context from DynamoDB
def get_context(thread_ts, user, default=""):
    if thread_ts is None:
        item = table.get_item(Key={"id": user}).get("Item")
    else:
        item = table.get_item(Key={"id": thread_ts}).get("Item")
    return (item["conversation"]) if item else (default)


# Put the context in DynamoDB
def put_context(thread_ts, user, conversation=""):
    expire_at = int(time.time()) + 3600  # 1h
    expire_dt = datetime.datetime.fromtimestamp(expire_at).isoformat()
    if thread_ts is None:
        table.put_item(
            Item={
                "id": user,
                "conversation": conversation,
                "expire_dt": expire_dt,
                "expire_at": expire_at,
            }
        )
    else:
        table.put_item(
            Item={
                "id": thread_ts,
                "conversation": conversation,
                "expire_dt": expire_dt,
                "expire_at": expire_at,
            }
        )


# Update the message in Slack
def chat_update(channel, ts, message, blocks=None):
    # print("chat_update: {}".format(message))
    app.client.chat_update(channel=channel, ts=ts, text=message, blocks=blocks)


def invoke_claude_3(messages):
    """
    Invokes Anthropic Claude 3 Sonnet to run an inference using the input
    provided in the request body.

    :param prompt: The prompt that you want Claude 3 to complete.
    :return: Inference response from the model.
    """

    text = ""

    try:
        body = {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": ANTHROPIC_TOKENS,
            "messages": messages,
        }

        response = bedrock.invoke_model(
            modelId=TEXT_MODEL_ID,
            body=json.dumps(body),
        )

        # Process and print the response
        result = json.loads(response.get("body").read())

        input_tokens = result["usage"]["input_tokens"]
        output_tokens = result["usage"]["output_tokens"]
        output_list = result.get("content", [])

        print(f"- The input length is {input_tokens} tokens.")
        print(f"- The output length is {output_tokens} tokens.")

        print(f"- The model returned {len(output_list)} response(s):")

        for output in output_list:
            text = output["text"]

        return text

    except Exception as e:
        print("Error: {}".format(e))


# Reply to the message
def reply_text(messages, channel, ts, user):
    message = invoke_claude_3(messages)

    chat_update(channel, ts, message)

    return message


# Get thread messages using conversations.replies API method
def conversations_replies(
    channel, ts, client_msg_id, messages=[], message_max=MESSAGE_MAX
):
    try:
        response = app.client.conversations_replies(channel=channel, ts=ts)

        print("conversations_replies: {}".format(response))

        if not response.get("ok"):
            print(
                "conversations_replies: {}".format(
                    "Failed to retrieve thread messages."
                )
            )

        res_messages = response.get("messages", [])
        res_messages.reverse()
        res_messages.pop(0)  # remove the first message

        for message in res_messages:
            if message.get("client_msg_id", "") == client_msg_id:
                continue

            role = "user"
            if message.get("bot_id", "") != "":
                role = "assistant"

            messages.append(
                {
                    "role": role,
                    "content": message.get("text", ""),
                }
            )

            # print("conversations_replies: messages size: {}".format(sys.getsizeof(messages)))

            if sys.getsizeof(messages) > message_max:
                messages.pop(0)  # remove the oldest message
                break

    except Exception as e:
        print("conversations_replies: {}".format(e))

    if SYSTEM_MESSAGE != "None":
        messages.append(
            {
                "role": "system",
                "content": SYSTEM_MESSAGE,
            }
        )

    print("conversations_replies: {}".format(messages))

    return messages


# Handle the chatgpt conversation
def conversation(say: Say, thread_ts, content, channel, user, client_msg_id):
    print("conversation: {}".format(json.dumps(content)))

    # Keep track of the latest message timestamp
    result = say(text=BOT_CURSOR, thread_ts=thread_ts)
    latest_ts = result["ts"]

    messages = []
    messages.append(
        {
            "role": "user",
            "content": content,
        },
    )

    # Get the thread messages
    if thread_ts != None:
        chat_update(channel, latest_ts, "이전 대화 내용 확인 중... " + BOT_CURSOR)

        messages = conversations_replies(channel, thread_ts, client_msg_id, messages)

        messages = messages[::-1]  # reversed

    # Send the prompt to ChatGPT
    try:
        print("conversation: {}".format(messages))

        # Send the prompt to ChatGPT
        message = reply_text(messages, channel, latest_ts, user)

        print("conversation: {}".format(message))

    except Exception as e:
        print("conversation: Error handling message: {}".format(e))

        message = f"```{e}```"

        chat_update(channel, latest_ts, message)


# Extract content from the message
def content_from_message(prompt, event):
    content = []
    content.append({"type": "text", "text": prompt})

    return content


# Handle the app_mention event
@app.event("app_mention")
def handle_mention(body: dict, say: Say):
    print("handle_mention: {}".format(body))

    event = body["event"]

    if "bot_id" in event:  # Ignore messages from the bot itself
        # print("handle_mention: {}".format("Ignore messages from the bot itself"))
        return

    thread_ts = event["thread_ts"] if "thread_ts" in event else event["ts"]
    prompt = re.sub(f"<@{bot_id}>", "", event["text"]).strip()
    channel = event["channel"]
    user = event["user"]
    client_msg_id = event["client_msg_id"]

    content = content_from_message(prompt, event)

    conversation(say, thread_ts, content, channel, user, client_msg_id)


# Handle the DM (direct message) event
@app.event("message")
def handle_message(body: dict, say: Say):
    print("handle_message: {}".format(body))

    event = body["event"]

    if "bot_id" in event:  # Ignore messages from the bot itself
        # print("handle_mention: {}".format("Ignore messages from the bot itself"))
        return

    prompt = event["text"].strip()
    channel = event["channel"]
    user = event["user"]
    client_msg_id = event["client_msg_id"]

    content = content_from_message(prompt, event)

    # Use thread_ts=None for regular messages, and user ID for DMs
    conversation(say, None, content, channel, user, client_msg_id)


# Handle the Lambda function
def lambda_handler(event, context):
    body = json.loads(event["body"])

    if "challenge" in body:
        # Respond to the Slack Event Subscription Challenge
        return {
            "statusCode": 200,
            "headers": {"Content-type": "application/json"},
            "body": json.dumps({"challenge": body["challenge"]}),
        }

    print("lambda_handler: {}".format(body))

    # Duplicate execution prevention
    if "event" not in body or "client_msg_id" not in body["event"]:
        return {
            "statusCode": 200,
            "headers": {"Content-type": "application/json"},
            "body": json.dumps({"status": "Success"}),
        }

    # Get the context from DynamoDB
    token = body["event"]["client_msg_id"]
    prompt = get_context(token, body["event"]["user"])

    if prompt != "":
        return {
            "statusCode": 200,
            "headers": {"Content-type": "application/json"},
            "body": json.dumps({"status": "Success"}),
        }

    # Put the context in DynamoDB
    put_context(token, body["event"]["user"], body["event"]["text"])

    # Handle the event
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)
