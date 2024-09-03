# bedrock

## Install

```bash
$ brew install python@3.11

$ python -m pip install --upgrade -r requirements.txt
```

## Test

```bash
python invoke_claude_3.py -p "구름이가 누구?"

python invoke_claude_3_image.py

python invoke_stable_diffusion.py -p "Create an image of a cat walking on a fully frozen river surface on a cold winter day."
```

## References

* <https://docs.aws.amazon.com/ko_kr/code-library/latest/ug/python_3_bedrock-runtime_code_examples.html>
* <https://docs.aws.amazon.com/ko_kr/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html>
