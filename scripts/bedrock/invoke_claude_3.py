#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3
import os


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

ANTHROPIC_VERSION = os.environ.get("ANTHROPIC_VERSION", "bedrock-2023-05-31")
ANTHROPIC_TOKENS = int(os.environ.get("ANTHROPIC_TOKENS", 1024))

MODEL_ID_TEXT = "anthropic.claude-3-5-sonnet-20240620-v1:0"

# Set up System messages
PERSONAL_MESSAGE = os.environ.get(
    "PERSONAL_MESSAGE", "당신은 친절하고 전문적인 AI 비서 입니다."
)
SYSTEM_MESSAGE = "답변을 할때 참고한 문서가 있다면 링크도 알려줘."


# Initialize the Amazon Bedrock runtime client
bedrock = boto3.client(service_name="bedrock-runtime", region_name=AWS_REGION)


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="안녕", help="prompt")
    return p.parse_args()


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
