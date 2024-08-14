#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3
import pprint

from botocore.client import Config

pp = pprint.PrettyPrinter(indent=2)

bedrock_config = Config(
    connect_timeout=120, read_timeout=120, retries={"max_attempts": 0}
)
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
bedrock_agent_client = boto3.client(
    "bedrock-agent-runtime", region_name="us-east-1", config=bedrock_config
)
# boto3_session = boto3.session.Session()
# region_name = boto3_session.region_name

model_id = "anthropic.claude-v2:1"  # try with both claude instant as well as claude-v2. for claude v2 - "anthropic.claude-v2"
region_id = "us-east-1"  # replace it with the region you're running sagemaker notebook

SYSTEM_MESSAGE = "답변은 한국어 해요체로 해요."


# def parse_args():
#     p = argparse.ArgumentParser(description="invoke_knowledge_base")
#     p.add_argument("-p", "--prompt", default="안녕", help="prompt")
#     p.add_argument("-d", "--debug", default="False", help="debug")
#     return p.parse_args()


def retrieve(query, kbId, numberOfResults=5):
    return bedrock_agent_client.retrieve(
        retrievalQuery={"text": query},
        knowledgeBaseId=kbId,
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": numberOfResults,
                # "overrideSearchType": "HYBRID",  # optional
            }
        },
    )


def retrieveAndGenerate(
    input,
    kbId,
    sessionId=None,
    model_id="anthropic.claude-v2:1",
    region_id="us-east-1",
):
    model_arn = f"arn:aws:bedrock:{region_id}::foundation-model/{model_id}"
    if sessionId:
        return bedrock_agent_client.retrieve_and_generate(
            input={"text": input},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kbId,
                    "modelArn": model_arn,
                },
            },
            sessionId=sessionId,
        )
    else:
        return bedrock_agent_client.retrieve_and_generate(
            input={"text": input},
            retrieveAndGenerateConfiguration={
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": kbId,
                    "modelArn": model_arn,
                },
            },
        )


def main():
    # args = parse_args()

    kb_id = "knowledge-base-whitepaper"

    query = "Please tell me about the Kontrol."

    # response = retrieve(query, kb_id, 3)
    # retrievalResults = response["retrievalResults"]
    # pp.pprint(retrievalResults)

    response = retrieveAndGenerate(query, kb_id, model_id=model_id, region_id=region_id)
    generated_text = response["output"]["text"]
    pp.pprint(generated_text)


if __name__ == "__main__":
    main()
