#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="Hello", help="prompt", required=True)
    p.add_argument("-d", "--debug", default="False", help="debug")
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

    try:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }

        response = bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
        )

        # Process and print the response
        result = json.loads(response.get("body").read())
        input_tokens = result["usage"]["input_tokens"]
        output_tokens = result["usage"]["output_tokens"]
        output_list = result.get("content", [])

        print("Invocation details:")
        print(f"- The input length is {input_tokens} tokens.")
        print(f"- The output length is {output_tokens} tokens.")

        print(f"- The model returned {len(output_list)} response(s):")

        for output in output_list:
            print("=")
            print(output["text"])

        return result

    except Exception as e:
        print("Error: {}".format(e))


def main():
    args = parse_args()

    invoke_claude_3(args.prompt)


if __name__ == "__main__":
    main()
