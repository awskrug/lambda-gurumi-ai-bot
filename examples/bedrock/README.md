# Bedrock 예제

## Install

```bash
$ brew install python@3.12

$ python -m pip install --upgrade -r requirements.txt
```

## Test

```bash
# Bedrock Agent 테스트
python invoke_agent.py -p "AWS DeepRacer 설명해줘"

# Claude 3 모델 직접 호출
python invoke_claude_3.py -p "구름이가 누구?"

# Claude 3 이미지 분석
python invoke_claude_3_image.py

# 이미지 생성 (Stable Diffusion)
python invoke_stable_diffusion.py -p "Create an image of a cat walking on a fully frozen river surface on a cold winter day."

# Knowledge Base 쿼리
python invoke_knowledge_base.py -p "지식 베이스 쿼리"

# 스트리밍 대화
python converse_stream.py -p "프롬프트 입력"
```

## References

* <https://docs.aws.amazon.com/ko_kr/code-library/latest/ug/python_3_bedrock-runtime_code_examples.html>
* <https://docs.aws.amazon.com/ko_kr/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html>
