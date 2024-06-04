#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import boto3
import base64
import io

from PIL import Image


def parse_args():
    p = argparse.ArgumentParser(description="invoke_claude_3")
    p.add_argument("-p", "--prompt", default="Hello", help="prompt", required=True)
    p.add_argument("-d", "--debug", default="False", help="debug")
    return p.parse_args()


def invoke_stable_diffusion(prompt, seed=0, style_preset="photographic"):
    """
    Invokes the Stability.ai Stable Diffusion XL model to create an image using
    the input provided in the request body.

    :param prompt: The prompt that you want Stable Diffusion  to use for image generation.
    :param seed: Random noise seed (omit this option or use 0 for a random seed)
    :param style_preset: Pass in a style preset to guide the image model towards
                          a particular style.
    :return: Base64-encoded inference response from the model.
    """

    # Initialize the Amazon Bedrock runtime client
    bedrock = boto3.client(service_name="bedrock-runtime", region_name="us-east-1")

    # Invoke Claude 3 with the text prompt
    model_id = "stability.stable-diffusion-xl-v1"

    try:
        # The different model providers have individual request and response formats.
        # For the format, ranges, and available style_presets of Stable Diffusion models refer to:
        # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-stability-diffusion.html

        body = {
            "text_prompts": [{"text": prompt}],
            "seed": seed,
            "cfg_scale": 10,
            "steps": 30,
            "samples": 1,
        }

        if style_preset:
            body["style_preset"] = style_preset

        response = bedrock.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
        )

        response_body = json.loads(response["body"].read())
        base64_image = response_body.get("artifacts")[0].get("base64")
        base64_bytes = base64_image.encode("ascii")
        image_bytes = base64.b64decode(base64_bytes)

        image = Image.open(io.BytesIO(image_bytes))
        image.show()

    except Exception as e:
        print("Error: {}".format(e))


def main():
    args = parse_args()

    invoke_stable_diffusion(args.prompt)


if __name__ == "__main__":
    main()
