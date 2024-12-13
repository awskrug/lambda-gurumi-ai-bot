import boto3
import json
import os
import re
import sys
import time

from datetime import datetime, timezone, timedelta

from botocore.client import Config

from slack_bolt import App, Say
from slack_bolt.adapter.aws_lambda import SlackRequestHandler


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Set up Slack API credentials
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]

# Keep track of conversation history by thread and user
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "gurumi-bot-context")

# Amazon Bedrock Agent ID
AGENT_ID = os.environ.get("AGENT_ID", "None")
AGENT_ALIAS_ID = os.environ.get("AGENT_ALIAS_ID", "None")

# # Amazon Bedrock Knowledge Base ID
# KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "None")

# KB_RETRIEVE_COUNT = int(os.environ.get("KB_RETRIEVE_COUNT", 5))

# # Amazon Bedrock Model ID
# ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
# ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 2000))

# MODEL_ID_TEXT = os.environ.get("MODEL_ID_TEXT", "anthropic.claude-3")
# MODEL_ID_IMAGE = os.environ.get("MODEL_ID_IMAGE", "stability.stable-diffusion-xl")

# Set up the allowed channel ID
ALLOWED_CHANNEL_IDS = os.environ.get("ALLOWED_CHANNEL_IDS", "None")

# Set up System messages
PERSONAL_MESSAGE = os.environ.get(
    "PERSONAL_MESSAGE", "You are a friendly and professional AI assistant."
)
SYSTEM_MESSAGE = os.environ.get("SYSTEM_MESSAGE", "None")

MAX_LEN_SLACK = int(os.environ.get("MAX_LEN_SLACK", 3000))
MAX_LEN_BEDROCK = int(os.environ.get("MAX_LEN_BEDROCK", 4000))

SLACK_SAY_INTERVAL = float(os.environ.get("SLACK_SAY_INTERVAL", 0))

BOT_CURSOR = os.environ.get("BOT_CURSOR", ":robot_face:")

MSG_KNOWLEDGE = "지식 기반 검색 중... " + BOT_CURSOR
MSG_PREVIOUS = "이전 대화 내용 확인 중... " + BOT_CURSOR
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
bedrock_client = boto3.client(service_name="bedrock-runtime", region_name=AWS_REGION)

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
    expire_dt = datetime.fromtimestamp(expire_at).isoformat()
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


def split_message(message, max_len):
    split_parts = []

    # 먼저 ``` 기준으로 분리
    parts = message.split("```")

    for i, part in enumerate(parts):
        if i % 2 == 1:  # 코드 블록인 경우
            # 코드 블록도 "\n\n" 기준으로 자름
            split_parts.extend(split_code_block(part, max_len))
        else:  # 일반 텍스트 부분
            split_parts.extend(split_by_newline(part, max_len))

    # 전체 블록을 합친 후 max_len을 넘지 않도록 추가로 자름
    return finalize_split(split_parts, max_len)


def split_code_block(code, max_len):
    # 코드 블록을 "\n\n" 기준으로 분리 후, 다시 ```로 감쌈
    code_parts = code.split("\n\n")
    result = []
    current_part = "```\n"

    for part in code_parts:
        if len(current_part) + len(part) + 2 < max_len - 6:  # 6은 ``` 앞뒤 길이
            if current_part != "```\n":
                current_part += "\n\n" + part
            else:
                current_part += part
        else:
            result.append(current_part + "\n```")  # ```로 감쌈
            current_part = "```\n" + part

    if current_part != "```\n":
        result.append(current_part + "\n```")

    return result


def split_by_newline(text, max_len):
    # "\n\n" 기준으로 분리
    parts = text.split("\n\n")
    result = []
    current_part = ""

    for part in parts:
        if len(current_part) + len(part) + 2 < max_len:  # 2는 "\n\n"의 길이
            if current_part != "":
                current_part += "\n\n" + part
            else:
                current_part = part
        else:
            result.append(current_part)
            current_part = part
    if current_part != "":
        result.append(current_part)

    return result


def finalize_split(parts, max_len):
    # 각 파트를 max_len에 맞춰 추가로 자름
    result = []
    current_message = ""

    for part in parts:
        if len(current_message) + len(part) < max_len:
            current_message += "\n\n" + part
        else:
            result.append(current_message)
            current_message = part
    if current_message != "":
        result.append(current_message)

    return result


# Update the message in Slack
def chat_update(say, channel, thread_ts, latest_ts, message="", continue_thread=False):
    # print("chat_update: {}".format(message))

    split_messages = split_message(message, MAX_LEN_SLACK)

    for i, text in enumerate(split_messages):
        if i == 0:
            # Update the message
            app.client.chat_update(channel=channel, ts=latest_ts, text=text)
        else:
            if SLACK_SAY_INTERVAL > 0:
                time.sleep(SLACK_SAY_INTERVAL)

            try:
                # Send a new message
                result = say(text=text, thread_ts=thread_ts)
                latest_ts = result["ts"]
            except Exception as e:
                print("chat_update: Error: {}".format(e))

    return message, latest_ts


# Get thread messages using conversations.replies API method
def conversations_replies(channel, ts, client_msg_id):
    contexts = []

    try:
        response = app.client.conversations_replies(channel=channel, ts=ts)

        print("conversations_replies: {}".format(response))

        if not response.get("ok"):
            print(
                "conversations_replies: {}".format(
                    "Failed to retrieve thread messages."
                )
            )

        messages = response.get("messages", [])
        messages.reverse()
        messages.pop(0)  # remove the first message

        for message in messages:
            if message.get("client_msg_id", "") == client_msg_id:
                continue

            role = "user"
            if message.get("bot_id", "") != "":
                role = "assistant"

            contexts.append("{}: {}".format(role, message.get("text", "")))

            if sys.getsizeof(contexts) > MAX_LEN_BEDROCK:
                contexts.pop(0)  # remove the oldest message
                break

        contexts.reverse()

    except Exception as e:
        print("conversations_replies: Error: {}".format(e))

    print("conversations_replies: getsizeof: {}".format(sys.getsizeof(contexts)))
    # print("conversations_replies: {}".format(contexts))

    return contexts


# def invoke_knowledge_base(content):
#     """
#     Invokes the Amazon Bedrock Knowledge Base to retrieve information using the input
#     provided in the request body.

#     :param content: The content that you want to use for retrieval.
#     :return: The retrieved contexts from the knowledge base.
#     """

#     contexts = []

#     if KNOWLEDGE_BASE_ID == "None":
#         return contexts

#     try:
#         response = bedrock_agent_client.retrieve(
#             retrievalQuery={"text": content},
#             knowledgeBaseId=KNOWLEDGE_BASE_ID,
#             retrievalConfiguration={
#                 "vectorSearchConfiguration": {
#                     "numberOfResults": KB_RETRIEVE_COUNT,
#                     # "overrideSearchType": "HYBRID",  # optional
#                 }
#             },
#         )

#         results = response["retrievalResults"]

#         contexts = []
#         for result in results:
#             contexts.append(result["content"]["text"])

#     except Exception as e:
#         print("invoke_knowledge_base: Error: {}".format(e))

#     print("invoke_knowledge_base: {}".format(contexts))

#     return contexts


def invoke_agent(prompt):
    """
    Sends a prompt for the agent to process and respond to.

    :param agent_id: The unique identifier of the agent to use.
    :param agent_alias_id: The alias of the agent to use.
    :param session_id: The unique identifier of the session. Use the same value across requests
                        to continue the same conversation.
    :param prompt: The prompt that you want Claude to complete.
    :return: Inference response from the model.
    """

    now = datetime.now()
    session_id = str(int(now.timestamp() * 1000))

    try:
        # Note: The execution time depends on the foundation model, complexity of the agent,
        # and the length of the prompt. In some cases, it can take up to a minute or more to
        # generate a response.
        response = bedrock_agent_client.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=session_id,
            inputText=prompt,
        )

        completion = ""

        for event in response.get("completion"):
            chunk = event["chunk"]
            completion = completion + chunk["bytes"].decode()

    except Exception as e:
        print("invoke_agent: Error: {}".format(e))
        raise e

    return completion


# def invoke_claude_3(prompt):
#     """
#     Invokes Anthropic Claude 3 Sonnet to run an inference using the input
#     provided in the request body.

#     :param prompt: The prompt that you want Claude 3 to complete.
#     :return: Inference response from the model.
#     """

#     try:
#         body = {
#             "anthropic_version": ANTHROPIC_VERSION,
#             "max_tokens": ANTHROPIC_TOKENS,
#             "messages": [
#                 {
#                     "role": "user",
#                     "content": [{"type": "text", "text": prompt}],
#                 },
#             ],
#         }

#         response = bedrock_client.invoke_model(
#             modelId=MODEL_ID_TEXT,
#             body=json.dumps(body),
#         )

#         # Process and print the response
#         body = json.loads(response.get("body").read())

#         print("response: {}".format(body))

#         result = body.get("content", [])

#         for output in result:
#             text = output["text"]

#         return text

#     except Exception as e:
#         print("invoke_claude_3: Error: {}".format(e))
#         raise e


# Handle the chatgpt conversation
def conversation(say: Say, thread_ts, query, channel, client_msg_id):
    print("conversation: query: {}".format(query))

    # Keep track of the latest message timestamp
    result = say(text=BOT_CURSOR, thread_ts=thread_ts)
    latest_ts = result["ts"]

    prompts = []
    # prompts.append("User: {}".format(PERSONAL_MESSAGE))
    # prompts.append(
    #     "If you don't know the answer, just say that you don't know, don't try to make up an answer."
    # )

    # if SYSTEM_MESSAGE != "None":
    #     prompts.append(SYSTEM_MESSAGE)

    # prompts.append("<question> 태그로 감싸진 질문에 답변을 제공하세요.")

    try:
        # # Get the knowledge base contexts
        # if KNOWLEDGE_BASE_ID != "None":
        #     chat_update(say, channel, thread_ts, latest_ts, MSG_KNOWLEDGE)

        #     contexts = invoke_knowledge_base(query)

        #     prompts.append(
        #         "<context> 에 정보가 제공 되면, 해당 정보를 사용하여 답변해 주세요."
        #     )
        #     prompts.append("<context>")
        #     prompts.append("\n\n".join(contexts))
        #     prompts.append("</context>")
        # else:

        # Get the previous conversation contexts
        if thread_ts != None:
            chat_update(say, channel, thread_ts, latest_ts, MSG_PREVIOUS)

            contexts = conversations_replies(channel, thread_ts, client_msg_id)

            # prompts.append(
            #     "<history> 에 정보가 제공 되면, 대화 기록을 참고하여 답변해 주세요."
            # )
            prompts.append("<history>")
            prompts.append("\n\n".join(contexts))
            prompts.append("</history>")

        # Add the question to the prompts
        # prompts.append("")
        prompts.append("<question>")
        prompts.append(query)
        prompts.append("</question>")
        # prompts.append("")

        # prompts.append("Assistant:")

        # Combine the prompts
        prompt = "\n".join(prompts)

        # print("conversation: prompt: {}".format(prompt))

        chat_update(say, channel, thread_ts, latest_ts, MSG_RESPONSE)

        # Send the prompt to Bedrock
        message = invoke_agent(prompt)

        # if AGENT_ID != "None":
        #     message = invoke_agent(prompt)
        # else:
        #     message = invoke_claude_3(prompt)

        # print("conversation: message: {}".format(message))

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
    client_msg_id = event["client_msg_id"]

    if ALLOWED_CHANNEL_IDS != "None":
        allowed_channel_ids = ALLOWED_CHANNEL_IDS.split(",")
        if channel not in allowed_channel_ids:
            say(
                text="Sorry, I'm not allowed to respond in this channel.",
                thread_ts=thread_ts,
            )
            return

    prompt = re.sub(f"<@{bot_id}>", "", event["text"]).strip()

    conversation(say, thread_ts, prompt, channel, client_msg_id)


# Handle the DM (direct message) event
@app.event("message")
def handle_message(body: dict, say: Say):
    print("handle_message: {}".format(body))

    event = body["event"]

    if "bot_id" in event:
        # Ignore messages from the bot itself
        return

    channel = event["channel"]
    client_msg_id = event["client_msg_id"]

    prompt = event["text"].strip()

    # Use thread_ts=None for regular messages, and user ID for DMs
    conversation(say, None, prompt, channel, client_msg_id)


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
