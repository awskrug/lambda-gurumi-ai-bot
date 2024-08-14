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

from botocore.client import Config

from slack_bolt import App, Say
from slack_bolt.adapter.aws_lambda import SlackRequestHandler


BOT_CURSOR = os.environ.get("BOT_CURSOR", ":robot_face:")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Set up Slack API credentials
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# Keep track of conversation history by thread and user
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "gurumi-ai-bot-context")

# Amazon Bedrock Knowledge Base ID
KB_ID = os.environ.get("KB_ID", "None")

# Amazon Bedrock Model ID
MODEL_ID_TEXT = os.environ.get("MODEL_ID_TEXT", "anthropic.claude-3")
MODEL_ID_IMAGE = os.environ.get("MODEL_ID_IMAGE", "stability.stable-diffusion-xl")

ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 1024))

# Set up the allowed channel ID
ALLOWED_CHANNEL_IDS = os.environ.get("ALLOWED_CHANNEL_IDS", "None")

ENABLE_IMAGE = os.environ.get("ENABLE_IMAGE", "False")

# Set up System messages
SYSTEM_MESSAGE = os.environ.get("SYSTEM_MESSAGE", "None")

MAX_LEN_SLACK = int(os.environ.get("MAX_LEN_SLACK", 3000))
MAX_LEN_BEDROCK = int(os.environ.get("MAX_LEN_BEDROCK", 4000))

KEYWARD_IMAGE = "그려줘"

MSG_PREVIOUS = "이전 대화 내용 확인 중... " + BOT_CURSOR
MSG_IMAGE_DESCRIBE = "이미지 감상 중... " + BOT_CURSOR
MSG_IMAGE_GENERATE = "이미지 생성 준비 중... " + BOT_CURSOR
MSG_IMAGE_DRAW = "이미지 그리는 중... " + BOT_CURSOR
MSG_RESPONSE = "응답 기다리는 중... " + BOT_CURSOR

COMMAND_DESCRIBE = "Describe the image in great detail as if viewing a photo."
COMMAND_GENERATE = "Convert the above sentence into a command for stable-diffusion to generate an image within 1000 characters. Just give me a prompt."

CONVERSION_ARRAY = [
    ["**", "*"],
    # ["#### ", "🔸 "],
    # ["### ", "🔶 "],
    # ["## ", "🟠 "],
    # ["# ", "🟡 "],
]


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
bedrock = boto3.client(service_name="bedrock-runtime", region_name=AWS_REGION)

bedrock_config = Config(
    connect_timeout=120, read_timeout=120, retries={"max_attempts": 0}
)
bedrock_agent_client = boto3.client(
    "bedrock-agent-runtime", region_name=AWS_REGION, config=bedrock_config
)


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


# Replace text
def replace_text(text):
    for old, new in CONVERSION_ARRAY:
        text = text.replace(old, new)
    return text


# Update the message in Slack
def chat_update(say, channel, thread_ts, latest_ts, message="", continue_thread=False):
    # print("chat_update: {}".format(message))

    if sys.getsizeof(message) > MAX_LEN_SLACK:
        split_key = "\n\n"
        if "```" in message:
            split_key = "```"

        parts = message.split(split_key)

        last_one = parts.pop()

        if len(parts) % 2 == 0:
            text = split_key.join(parts) + split_key
            message = last_one
        else:
            text = split_key.join(parts)
            message = split_key + last_one

        text = replace_text(text)

        # Update the message
        app.client.chat_update(channel=channel, ts=latest_ts, text=text)

        if continue_thread:
            text = replace_text(message) + " " + BOT_CURSOR
        else:
            text = replace_text(message)

        # New message
        result = say(text=text, thread_ts=thread_ts)
        latest_ts = result["ts"]
    else:
        if continue_thread:
            text = replace_text(message) + " " + BOT_CURSOR
        else:
            text = replace_text(message)

        # Update the message
        app.client.chat_update(channel=channel, ts=latest_ts, text=text)

    return message, latest_ts


def invoke_knowledge_base(content):
    """
    Invokes the Amazon Bedrock Knowledge Base to retrieve information using the input
    provided in the request body.

    :param content: The content that you want to use for retrieval.
    :return: The retrieved contexts from the knowledge base.
    """

    try:
        response = bedrock_agent_client.retrieve(
            retrievalQuery={"text": content},
            knowledgeBaseId=KB_ID,
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 3,
                    # "overrideSearchType": "HYBRID",  # optional
                }
            },
        )

        retrievalResults = response["retrievalResults"]

        contexts = []
        for retrievedResult in retrievalResults:
            contexts.append(retrievedResult["content"]["text"])

        return contexts

    except Exception as e:
        print("invoke_knowledge_base: Error: {}".format(e))

        raise e


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
            modelId=MODEL_ID_TEXT,
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
            modelId=MODEL_ID_IMAGE,
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

            if sys.getsizeof(messages) > MAX_LEN_BEDROCK:
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
    if ENABLE_IMAGE == "True" and KEYWARD_IMAGE in prompt:
        type = "image"

    print("conversation: {}".format(type))

    prompts = []

    try:
        # Get the thread messages
        if thread_ts != None:
            chat_update(say, channel, thread_ts, latest_ts, MSG_PREVIOUS)

            replies = conversations_replies(channel, thread_ts, client_msg_id)

            prompts = [
                reply["content"] for reply in replies if reply["content"].strip()
            ]

        # Get the image from the message
        if type == "image" and len(content) > 1:
            chat_update(say, channel, thread_ts, latest_ts, MSG_IMAGE_DESCRIBE)

            content[0]["text"] = COMMAND_DESCRIBE

            # Send the prompt to Bedrock
            message = invoke_claude_3(content)

            prompts.append(message)

        if KB_ID != "None":
            chat_update(say, channel, thread_ts, latest_ts, MSG_RESPONSE)

            # Get the knowledge base contexts
            contexts = invoke_knowledge_base(prompt)

            prompts.extend(contexts)

        if prompt:
            prompts.append(prompt)

        if type == "image":
            chat_update(say, channel, thread_ts, latest_ts, MSG_IMAGE_GENERATE)

            prompts.append(COMMAND_GENERATE)

            prompt = "\n\n\n".join(prompts)

            content = []
            content.append({"type": "text", "text": prompt})

            # Send the prompt to Bedrock
            message = invoke_claude_3(content)

            chat_update(say, channel, thread_ts, latest_ts, MSG_IMAGE_DRAW)

            image = invoke_stable_diffusion(message)

            if image:
                # Update the message in Slack
                chat_update(say, channel, thread_ts, latest_ts, message)

                # Send the image to Slack
                app.client.files_upload_v2(
                    channels=channel,
                    thread_ts=thread_ts,
                    file=io.BytesIO(image),
                    filename="image.jpg",
                    title="Generated Image",
                    initial_comment="Here is the generated image.",
                )
        else:
            chat_update(say, channel, thread_ts, latest_ts, MSG_RESPONSE)

            prompt = "\n\n\n".join(prompts)

            content[0]["text"] = prompt

            # Send the prompt to Bedrock
            message = invoke_claude_3(content)

            # Update the message in Slack
            chat_update(say, channel, thread_ts, latest_ts, message)

    except Exception as e:
        print("conversation: Error: {}".format(e))

        chat_update(say, channel, thread_ts, latest_ts, f"```{e}```")


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

    # if "bot_id" in event and event["bot_id"] == bot_id:
    #     # Ignore messages from the bot itself
    #     return

    channel = event["channel"]

    if ALLOWED_CHANNEL_IDS != "None":
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
