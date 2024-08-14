#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3

from botocore.client import Config


bedrock_config = Config(
    connect_timeout=120, read_timeout=120, retries={"max_attempts": 0}
)
bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
bedrock_agent_client = boto3.client(
    "bedrock-agent-runtime", region_name="us-east-1", config=bedrock_config
)

model_id = "anthropic.claude-v2:1"  # try with both claude instant as well as claude-v2. for claude v2 - "anthropic.claude-v2"
region_id = "us-east-1"  # replace it with the region you're running sagemaker notebook

SYSTEM_MESSAGE = "답변은 한국어 해요체로 해요."


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="안녕", help="prompt")
    p.add_argument("-d", "--debug", default="False", help="debug")
    return p.parse_args()


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


# fetch context from the response
def get_contexts(retrievalResults):
    contexts = []
    for retrievedResult in retrievalResults:
        contexts.append(retrievedResult["content"]["text"])
    return contexts


def main():
    # args = parse_args()

    kb_id = "DQXVNP05K5"

    query = "kontrol의 기능 알려줘."

    # response = retrieveAndGenerate(query, kb_id, model_id=model_id, region_id=region_id)
    # generated_text = response["output"]["text"]
    # print(generated_text)

    response = retrieve(query, kb_id, 3)
    retrievalResults = response["retrievalResults"]
    # print(retrievalResults)

    contexts = get_contexts(retrievalResults)
    # print(contexts)

    prompt = f"""
Human: You are a financial advisor AI system, and provides answers to questions by using fact based and statistical information when possible.
Use the following pieces of information to provide a concise answer to the question enclosed in <question> tags.
If you don't know the answer, just say that you don't know, don't try to make up an answer.
<context>
{contexts}
</context>

<question>
{query}
</question>

The response should be specific and use statistics or numbers when possible.

Assistant:"""

    # payload with model paramters
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        }
    ]
    sonnet_payload = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "messages": messages,
            "temperature": 0.5,
            "top_p": 1,
        }
    )

    modelId = "anthropic.claude-3-sonnet-20240229-v1:0"  # change this to use a different version from the model provider
    accept = "application/json"
    contentType = "application/json"
    response = bedrock_client.invoke_model(
        body=sonnet_payload, modelId=modelId, accept=accept, contentType=contentType
    )
    response_body = json.loads(response.get("body").read())
    response_text = response_body.get("content")[0]["text"]

    print(response_text)


if __name__ == "__main__":
    main()
