import boto3
import datetime
import json
import os
import re
import sys
import time

from botocore.client import Config

from slack_bolt import App, Say
from slack_bolt.adapter.aws_lambda import SlackRequestHandler


BOT_CURSOR = os.environ.get("BOT_CURSOR", ":robot_face:")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Set up Slack API credentials
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# Keep track of conversation history by thread and user
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "gurumi-bot-context")

# Amazon Bedrock Knowledge Base ID
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "None")

KB_RETRIEVE_COUNT = int(os.environ.get("KB_RETRIEVE_COUNT", 5))

# Amazon Bedrock Model ID
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 1024))

MODEL_ID_TEXT = os.environ.get("MODEL_ID_TEXT", "anthropic.claude-3")
MODEL_ID_IMAGE = os.environ.get("MODEL_ID_IMAGE", "stability.stable-diffusion-xl")

# Set up the allowed channel ID
ALLOWED_CHANNEL_IDS = os.environ.get("ALLOWED_CHANNEL_IDS", "None")

# Set up System messages
SYSTEM_MESSAGE = os.environ.get("SYSTEM_MESSAGE", "None")

MAX_LEN_SLACK = int(os.environ.get("MAX_LEN_SLACK", 3000))
MAX_LEN_BEDROCK = int(os.environ.get("MAX_LEN_BEDROCK", 4000))

MSG_PREVIOUS = "지식 기반 검색 중... " + BOT_CURSOR
MSG_RESPONSE = "응답 기다리는 중... " + BOT_CURSOR

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

    if KNOWLEDGE_BASE_ID == "None":
        return ""

    try:
        response = bedrock_agent_client.retrieve(
            retrievalQuery={"text": content},
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": KB_RETRIEVE_COUNT,
                    # "overrideSearchType": "HYBRID",  # optional
                }
            },
        )

        results = response["retrievalResults"]

        contexts = []
        for result in results:
            contexts.append(result["content"]["text"])

        return "\n".join(contexts)

    except Exception as e:
        print("invoke_knowledge_base: Error: {}".format(e))

        raise e


def invoke_claude_3(prompt):
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
                    "content": [{"type": "text", "text": prompt}],
                },
            ],
        }

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


def gen_prompt(query, contexts):
    if contexts == "":
        prompt = f"""
Human: You are a advisor AI system, and provides answers to questions by using fact based and statistical information when possible.
If you don't know the answer, just say that you don't know, don't try to make up an answer.
{SYSTEM_MESSAGE}

<question>
{query}
</question>

The response should be specific and use statistics or numbers when possible.

Assistant:"""

    else:
        prompt = f"""
Human: You are a advisor AI system, and provides answers to questions by using fact based and statistical information when possible.
Use the following pieces of information to provide a concise answer to the question enclosed in <question> tags.
If you don't know the answer, just say that you don't know, don't try to make up an answer.
{SYSTEM_MESSAGE}
<context>
{contexts}
</context>

<question>
{query}
</question>

The response should be specific and use statistics or numbers when possible.

Assistant:"""

    return prompt


# Handle the chatgpt conversation
def conversation(say: Say, thread_ts, query, channel):
    print("conversation: query: {}".format(query))

    # Keep track of the latest message timestamp
    result = say(text=BOT_CURSOR, thread_ts=thread_ts)
    latest_ts = result["ts"]

    try:
        chat_update(say, channel, thread_ts, latest_ts, MSG_PREVIOUS)

        # Get the knowledge base contexts
        contexts = invoke_knowledge_base(query)

        print("conversation: contexts: {}".format(contexts))

        # Generate the prompt
        prompt = gen_prompt(query, contexts)

        # print("conversation: prompt: {}".format(prompt))

        chat_update(say, channel, thread_ts, latest_ts, MSG_RESPONSE)

        # Send the prompt to Bedrock
        message = invoke_claude_3(prompt)

        print("conversation: message: {}".format(message))

        # Update the message in Slack
        chat_update(say, channel, thread_ts, latest_ts, message)

    except Exception as e:
        print("conversation: error: {}".format(e))

        chat_update(say, channel, thread_ts, latest_ts, f"```{e}```")


# Handle the app_mention event
@app.event("app_mention")
def handle_mention(body: dict, say: Say):
    print("handle_mention: {}".format(body))

    event = body["event"]

    # if "bot_id" in event and event["bot_id"] == bot_id:
    #     # Ignore messages from the bot itself
    #     return

    thread_ts = event["thread_ts"] if "thread_ts" in event else event["ts"]

    channel = event["channel"]

    if ALLOWED_CHANNEL_IDS != "None":
        allowed_channel_ids = ALLOWED_CHANNEL_IDS.split(",")
        if channel not in allowed_channel_ids:
            say(
                text="Sorry, I'm not allowed to respond in this channel.",
                thread_ts=thread_ts,
            )
            return

    prompt = re.sub(f"<@{bot_id}>", "", event["text"]).strip()

    conversation(say, thread_ts, prompt, channel)


# Handle the DM (direct message) event
@app.event("message")
def handle_message(body: dict, say: Say):
    print("handle_message: {}".format(body))

    event = body["event"]

    if "bot_id" in event:
        # Ignore messages from the bot itself
        return

    channel = event["channel"]

    prompt = event["text"].strip()

    # Use thread_ts=None for regular messages, and user ID for DMs
    conversation(say, None, prompt, channel)


def success():
    return {
        "statusCode": 200,
        "headers": {"Content-type": "application/json"},
        "body": json.dumps({"status": "Success"}),
    }


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
        return success()

    # Get the context from DynamoDB
    token = body["event"]["client_msg_id"]
    prompt = get_context(token, body["event"]["user"])

    if prompt != "":
        return success()

    # Put the context in DynamoDB
    put_context(token, body["event"]["user"], body["event"]["text"])

    # Handle the event
    slack_handler = SlackRequestHandler(app=app)
    return slack_handler.handle(event, context)
