#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3
import os

from botocore.client import Config


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Amazon Bedrock Knowledge Base ID
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "IYEMTLJ9MM")

KB_RETRIEVE_COUNT = int(os.environ.get("KB_RETRIEVE_COUNT", 5))

# Amazon Bedrock Model ID
ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 1024))

MODEL_ID_TEXT = "anthropic.claude-3-sonnet-20240229-v1:0"

# Set up System messages
PERSONAL_MESSAGE = os.environ.get(
    "PERSONAL_MESSAGE", "당신은 친절하고 전문적인 AI 비서 입니다."
)
SYSTEM_MESSAGE = "답변을 할때 참고한 문서가 있다면 링크도 알려줘."


# Initialize the Amazon Bedrock runtime client
bedrock = boto3.client(service_name="bedrock-runtime", region_name=AWS_REGION)

bedrock_config = Config(
    connect_timeout=120, read_timeout=120, retries={"max_attempts": 0}
)
bedrock_agent_client = boto3.client(
    "bedrock-agent-runtime", region_name=AWS_REGION, config=bedrock_config
)


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="안녕", help="prompt")
    p.add_argument("-d", "--debug", default="False", help="debug")
    return p.parse_args()


def invoke_knowledge_base(content):
    """
    Invokes the Amazon Bedrock Knowledge Base to retrieve information using the input
    provided in the request body.

    :param content: The content that you want to use for retrieval.
    :return: The retrieved contexts from the knowledge base.
    """

    contexts = []

    if KNOWLEDGE_BASE_ID == "None":
        return contexts

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

    except Exception as e:
        print("invoke_knowledge_base: Error: {}".format(e))

    print("invoke_knowledge_base: {}".format("\n\n".join(contexts)))

    return contexts


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


def main():
    args = parse_args()

    query = args.prompt

    prompts = []
    prompts.append("User: {}".format(PERSONAL_MESSAGE))
    prompts.append("If you don't know the answer, just say that you don't know, don't try to make up an answer.")

    if SYSTEM_MESSAGE != "None":
        prompts.append(SYSTEM_MESSAGE)

    prompts.append("<question> 태그로 감싸진 질문에 답변을 제공하세요.")

    try:
        # Get the knowledge base contexts
        if KNOWLEDGE_BASE_ID != "None":
            contexts = invoke_knowledge_base(query)

            prompts.append(
                "<context> 에 정보가 제공 되면, 해당 정보를 사용하여 답변해 주세요."
            )
            prompts.append("<context>")
            prompts.append("\n\n".join(contexts))
            prompts.append("</context>")

        # Add the question to the prompts
        prompts.append("")
        prompts.append("<question>")
        prompts.append(query)
        prompts.append("</question>")
        prompts.append("")

        prompts.append("Assistant:")

        # Combine the prompts
        prompt = "\n".join(prompts)

        # Send the prompt to Bedrock
        message = invoke_claude_3(prompt)

        print("conversation: message: {}".format(message))

    except Exception as e:
        print("conversation: error: {}".format(e))


if __name__ == "__main__":
    main()
