#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import boto3
import os
import asyncio

from datetime import datetime

from botocore.exceptions import ClientError


AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

AGENT_ID = "LIKTFQ14NA"
AGENT_ALIAS_ID = "gurumi"


# Initialize the Amazon Bedrock agent runtime client
bedrock = boto3.client(service_name="bedrock-agent-runtime", region_name=AWS_REGION)


def parse_args():
    p = argparse.ArgumentParser(description="invoke_agent")
    p.add_argument("-p", "--prompt", default="안녕", help="prompt")
    p.add_argument("-d", "--debug", default="False", help="debug")
    return p.parse_args()


async def invoke_agent(prompt):
    """
    Sends a prompt for the agent to process and respond to.

    :param agent_id: The unique identifier of the agent to use.
    :param agent_alias_id: The alias of the agent to use.
    :param session_id: The unique identifier of the session. Use the same value across requests
                        to continue the same conversation.
    :param prompt: The prompt that you want Claude to complete.
    :return: Inference response from the model.
    """

    session_id = datetime.now().strftime("%Y%m%d%H%M%S")

    try:
        # Note: The execution time depends on the foundation model, complexity of the agent,
        # and the length of the prompt. In some cases, it can take up to a minute or more to
        # generate a response.
        response = bedrock.invoke_agent(
            agentId=AGENT_ID,
            agentAliasId=AGENT_ALIAS_ID,
            sessionId=session_id,
            inputText=prompt,
        )

        print(response)

        # completion = ""

        for event in response.get("completion"):
            print(event)

            yield event

            # chunk = event["chunk"]
            # completion = completion + chunk["bytes"].decode()

    except ClientError as e:
        print(f"Couldn't invoke agent. {e}")
        raise

    # return completion


async def main():
    args = parse_args()

    try:
        # Send the prompt to Bedrock Agent
        # message = invoke_agent(args.prompt)

        async for event in invoke_agent(args.prompt):
            print(f"Received event: {event}")

        # print(message)

    except Exception as e:
        print(f"error: {e}")


if __name__ == "__main__":
    # main()
    asyncio.run(main())
