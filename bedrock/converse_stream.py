#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3
import os


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

MODEL_ID_TEXT = "anthropic.claude-3-5-sonnet-20240620-v1:0"

# Set up System messages
PERSONAL_MESSAGE = os.environ.get(
    "PERSONAL_MESSAGE", "당신은 친절하고 전문적인 AI 비서 입니다."
)
SYSTEM_MESSAGE = "답변을 할때 참고한 문서가 있다면 링크도 알려줘."


# Initialize the Amazon Bedrock runtime client
bedrock = boto3.client(service_name="bedrock", region_name=AWS_REGION)

bedrock_runtime = boto3.client(service_name="bedrock-runtime", region_name=AWS_REGION)


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="안녕", help="prompt")
    return p.parse_args()


def create_inference_profile():
    profile_name = "gurumi-ai-bot-inference-profile"

    model_arn = "arn:aws:bedrock:{}::foundation-model/{}".format(
        AWS_REGION, MODEL_ID_TEXT
    )

    tags = [
        {"key": "Nmae", "value": "gurumi-ai-bot"},
        {"key": "dept", "value": "sre"},
    ]

    """Create Inference Profile using base model ARN"""
    response = bedrock.create_inference_profile(
        inferenceProfileName=profile_name,
        description="gurumi-ai-bot Inference Profile",
        modelSource={"copyFrom": model_arn},
        tags=tags,
    )

    profile_arn = response["inferenceProfileArn"]

    return profile_arn


def converse_stream(prompt):
    try:
        model_id = create_inference_profile()

        messages = [
            {
                "role": "user",
                "content": [{"text": prompt}],
            },
        ]

        streaming_response = bedrock_runtime.converse_stream(
            modelId=model_id,
            messages=messages,
            inferenceConfig={"maxTokens": 4096, "temperature": 0.5, "topP": 0.9},
        )

        # Extract and print the streamed response text in real-time.
        for chunk in streaming_response["stream"]:
            if "contentBlockDelta" in chunk:
                text = chunk["contentBlockDelta"]["delta"]["text"]
                print(text, end="")

    except Exception as e:
        print("converse_stream: Error: {}".format(e))

        raise e


def main():
    args = parse_args()

    query = args.prompt

    prompts = []
    prompts.append("User: {}".format(PERSONAL_MESSAGE))
    prompts.append(
        "If you don't know the answer, just say that you don't know, don't try to make up an answer."
    )

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
        converse_stream(prompt)

    except Exception as e:
        print("conversation: error: {}".format(e))


if __name__ == "__main__":
    main()
