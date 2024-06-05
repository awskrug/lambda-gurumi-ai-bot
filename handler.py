import boto3
import datetime
import json
import os
import re
import sys
import time
import base64
import requests
import io

from slack_bolt import App, Say
from slack_bolt.adapter.aws_lambda import SlackRequestHandler

BOT_CURSOR = os.environ.get("BOT_CURSOR", ":robot_face:")

# Set up Slack API credentials
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# Keep track of conversation history by thread and user
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "gurumi-ai-bot-context")

# Amazon Bedrock Model ID
TEXT_MODEL_ID = os.environ.get("TEXT_MODEL_ID", "anthropic.claude-3")
IMAGE_MODEL_ID = os.environ.get("IMAGE_MODEL_ID", "stability.stable-diffusion-xl")

ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 1024))

# Set up the allowed channel ID
ALLOWED_CHANNEL_IDS = os.environ.get("ALLOWED_CHANNEL_IDS", "")

ENABLE_IMAGE = os.environ.get("ENABLE_IMAGE", "False")

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


def invoke_claude_3(content):
    """
    Invokes Anthropic Claude 3 Sonnet to run an inference using the input
    provided in the request body.

    :param prompt: The prompt that you want Claude 3 to complete.
    :return: Inference response from the model.
    """

    try:
        body = {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": ANTHROPIC_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                },
            ],
        }

        if SYSTEM_MESSAGE != "None":
            body["system"] = SYSTEM_MESSAGE

        response = bedrock.invoke_model(
            modelId=TEXT_MODEL_ID,
            body=json.dumps(body),
        )

        # Process and print the response
        body = json.loads(response.get("body").read())

        print("response: {}".format(body))

        result = body.get("content", [])

        for output in result:
            text = output["text"]

        return text

    except Exception as e:
        print("invoke_claude_3: Error: {}".format(e))

        raise e


def invoke_stable_diffusion(prompt, seed=0, style_preset="photographic"):
    """
    Invokes the Stability.ai Stable Diffusion XL model to create an image using
    the input provided in the request body.

    :param prompt: The prompt that you want Stable Diffusion  to use for image generation.
    :param seed: Random noise seed (omit this option or use 0 for a random seed)
    :param style_preset: Pass in a style preset to guide the image model towards
                          a particular style.
    :return: Base64-encoded inference response from the model.
    """

    try:
        # The different model providers have individual request and response formats.
        # For the format, ranges, and available style_presets of Stable Diffusion models refer to:
        # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-stability-diffusion.html

        body = {
            "text_prompts": [{"text": prompt}],
            "seed": seed,
            "cfg_scale": 10,
            "steps": 30,
            "samples": 1,
        }

        if style_preset:
            body["style_preset"] = style_preset

        response = bedrock.invoke_model(
            modelId=IMAGE_MODEL_ID,
            body=json.dumps(body),
        )

        body = json.loads(response["body"].read())

        base64_image = body.get("artifacts")[0].get("base64")
        base64_bytes = base64_image.encode("ascii")

        image = base64.b64decode(base64_bytes)

        return image

    except Exception as e:
        print("invoke_stable_diffusion: Error: {}".format(e))

        raise e


# Get thread messages using conversations.replies API method
def conversations_replies(channel, ts, client_msg_id):
    messages = []

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

            if sys.getsizeof(messages) > MESSAGE_MAX:
                messages.pop(0)  # remove the oldest message
                break

        messages.reverse()

    except Exception as e:
        print("conversations_replies: {}".format(e))

    print("conversations_replies: {}".format(messages))

    return messages


# Handle the chatgpt conversation
def conversation(say: Say, thread_ts, content, channel, user, client_msg_id):
    print("conversation: {}".format(json.dumps(content)))

    # Keep track of the latest message timestamp
    result = say(text=BOT_CURSOR, thread_ts=thread_ts)
    latest_ts = result["ts"]

    prompt = content[0]["text"]

    type = "text"
    if ENABLE_IMAGE == "True" and "그려줘" in prompt:
        type = "image"

    print("conversation: {}".format(type))

    prompts = []

    try:
        # Get the thread messages
        if thread_ts != None:
            chat_update(channel, latest_ts, "이전 대화 내용 확인 중... " + BOT_CURSOR)

            replies = conversations_replies(channel, thread_ts, client_msg_id)

            prompts = [
                reply["content"] for reply in replies if reply["content"].strip()
            ]

        # Get the image from the message
        if type == "image" and len(content) > 1:
            chat_update(channel, latest_ts, "이미지 감상 중... " + BOT_CURSOR)

            content[0][
                "text"
            ] = "Describe the image in great detail as if viewing a photo."

            # Send the prompt to Bedrock
            message = invoke_claude_3(content)

            prompts.append(message)

        if prompt:
            prompts.append(prompt)

        if type == "image":
            chat_update(channel, latest_ts, "이미지 생성 준비 중... " + BOT_CURSOR)

            prompts.append(
                "Convert the above sentence into a command for stable-diffusion to generate an image within 1000 characters. Just give me a prompt."
            )

            prompt = "\n\n\n".join(prompts)

            content = []
            content.append({"type": "text", "text": prompt})

            # Send the prompt to Bedrock
            message = invoke_claude_3(content)

            chat_update(channel, latest_ts, "이미지 그리는 중... " + BOT_CURSOR)

            image = invoke_stable_diffusion(message)

            if image:
                # Update the message in Slack
                chat_update(channel, latest_ts, message)

                # Send the image to Slack
                app.client.files_upload_v2(
                    channels=channel,
                    file=io.BytesIO(image),
                    title="Generated Image",
                    filename="image.jpg",
                    initial_comment="Here is the generated image.",
                    thread_ts=latest_ts,
                )
        else:
            chat_update(channel, latest_ts, "응답 기다리는 중... " + BOT_CURSOR)

            prompt = "\n\n\n".join(prompts)

            content[0]["text"] = prompt

            # Send the prompt to Bedrock
            message = invoke_claude_3(content)

            # Update the message in Slack
            chat_update(channel, latest_ts, message.replace("**", "*"))

    except Exception as e:
        print("conversation: Error: {}".format(e))

        chat_update(channel, latest_ts, f"```{e}```")


# Get image from URL
def get_image_from_url(image_url, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(image_url, headers=headers)

    if response.status_code == 200:
        return response.content
    else:
        print("Failed to fetch image: {}".format(image_url))

    return None


# Get image from Slack
def get_image_from_slack(image_url):
    return get_image_from_url(image_url, SLACK_BOT_TOKEN)


# Get encoded image from Slack
def get_encoded_image_from_slack(image_url):
    image = get_image_from_slack(image_url)

    if image:
        return base64.b64encode(image).decode("utf-8")

    return None


# Extract content from the message
def content_from_message(prompt, event):
    content = []
    content.append({"type": "text", "text": prompt})

    if "files" in event:
        files = event.get("files", [])
        for file in files:
            mimetype = file["mimetype"]
            if mimetype.startswith("image"):
                image_url = file.get("url_private")
                base64_image = get_encoded_image_from_slack(image_url)
                if base64_image:
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mimetype,
                                "data": base64_image,
                            },
                        }
                    )

    return content


# Handle the app_mention event
@app.event("app_mention")
def handle_mention(body: dict, say: Say):
    print("handle_mention: {}".format(body))

    event = body["event"]

    if "bot_id" in event and event["bot_id"] == bot_id:
        # Ignore messages from the bot itself
        return

    channel = event["channel"]

    if ALLOWED_CHANNEL_IDS != "":
        allowed_channel_ids = ALLOWED_CHANNEL_IDS.split(",")
        if channel not in allowed_channel_ids:
            # say("Sorry, I'm not allowed to respond in this channel.")
            return

    thread_ts = event["thread_ts"] if "thread_ts" in event else event["ts"]
    user = event["user"]
    client_msg_id = event["client_msg_id"]

    prompt = re.sub(f"<@{bot_id}>", "", event["text"]).strip()

    content = content_from_message(prompt, event)

    conversation(say, thread_ts, content, channel, user, client_msg_id)


# Handle the DM (direct message) event
@app.event("message")
def handle_message(body: dict, say: Say):
    print("handle_message: {}".format(body))

    event = body["event"]

    if "bot_id" in event:
        # Ignore messages from the bot itself
        return

    channel = event["channel"]
    user = event["user"]
    client_msg_id = event["client_msg_id"]

    prompt = event["text"].strip()

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
