#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3
import base64


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="사진 설명 해줘", help="prompt")
    return p.parse_args()


def invoke_claude_3(prompt):
    """
    Invokes Anthropic Claude 3 Sonnet to run an inference using the input
    provided in the request body.

    :param prompt: The prompt that you want Claude 3 to complete.
    :return: Inference response from the model.
    """

    # Initialize the Amazon Bedrock runtime client
    bedrock = boto3.client(service_name="bedrock-runtime", region_name="us-east-1")

    # Invoke Claude 3 with the text prompt
    model_id = "anthropic.claude-3-sonnet-20240229-v1:0"

    image = "../images/gurumi-bot.png"

    # Read reference image from file and encode as base64 strings.
    with open(image, "rb") as file:
        encoded_image = base64.b64encode(file.read()).decode("utf8")

    try:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": encoded_image,
                            },
                        },
                    ],
                },
            ],
        }

        # print("request: {}".format(body))

        response = bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
        )

        # Process and print the response
        body = json.loads(response.get("body").read())

        # print("response: {}".format(body))

        content = body.get("content", [])

        for output in content:
            print(output["text"])

    except Exception as e:
        print("Error: {}".format(e))


def main():
    args = parse_args()

    invoke_claude_3(args.prompt)


if __name__ == "__main__":
    main()
