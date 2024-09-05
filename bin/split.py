MAX_LEN_SLACK = 400  # 슬랙 최대 메시지 길이 설정


def split_message(message, max_len):
    split_parts = []

    # 먼저 ``` 기준으로 분리
    parts = message.split("```")

    for i, part in enumerate(parts):
        if i % 2 == 1:  # 코드 블록인 경우
            # 코드 블록도 "\n\n" 기준으로 자름
            split_parts.extend(split_code_block(part, max_len))
        else:  # 일반 텍스트 부분
            split_parts.extend(split_by_newline(part, max_len))

    # 전체 블록을 합친 후 max_len을 넘지 않도록 추가로 자름
    return finalize_split(split_parts, max_len)


def split_code_block(code, max_len):
    # 코드 블록을 "\n\n" 기준으로 분리 후, 다시 ```로 감쌈
    code_parts = code.split("\n\n")
    result = []
    current_part = "```"

    for part in code_parts:
        if len(current_part) + len(part) + 2 < max_len - 6:  # 6은 ``` 앞뒤 길이
            if current_part != "```":
                current_part += "\n\n" + part
            else:
                current_part += part
        else:
            result.append(current_part + "\n```\n")  # ```로 감쌈
            current_part = "```\n\n" + part

    if current_part != "```":
        result.append(current_part + "\n```\n")

    return result


def split_by_newline(text, max_len):
    # "\n\n" 기준으로 분리
    parts = text.split("\n\n")
    result = []
    current_part = ""

    for part in parts:
        if len(current_part) + len(part) + 2 < max_len:  # 2는 "\n\n"의 길이
            if current_part:
                current_part += "\n\n" + part
            else:
                current_part = part
        else:
            result.append(current_part)
            current_part = part
    if current_part:
        result.append(current_part)

    return result


def finalize_split(parts, max_len):
    # 각 파트를 max_len에 맞춰 추가로 자름
    result = []
    current_message = ""

    for part in parts:
        if len(current_message) + len(part) < max_len:
            current_message += part
        else:
            result.append(current_message)
            current_message = part
    if current_message:
        result.append(current_message)

    return result


# 테스트
message = """
JSON 데이터를 정렬하는 방법은 사용하는 프로그래밍 언어나 도구에 따라 다를 수 있습니다. 여기서는 Python을 사용한 예시를 보여드리겠습니다. Python의 json 모듈을 사용하면 쉽게 JSON 데이터를 정렬할 수 있습니다.

```python
import json

# 정렬되지 않은 JSON 데이터
data = {
    "name": "Alice",
    "age": 30,
    "city": "New York",
    "hobbies": ["reading", "hiking", "coding"]
}

# JSON 데이터를 정렬하여 문자열로 변환
sorted_json_str = json.dumps(data, indent=4, sort_keys=True)

# 정렬되지 않은 JSON 데이터
data = {
    "name": "Alice",
    "age": 30,
    "city": "New York",
    "hobbies": ["reading", "hiking", "coding"]
}

# JSON 데이터를 정렬하여 문자열로 변환
sorted_json_str = json.dumps(data, indent=4, sort_keys=True)

# 정렬된 JSON 출력
print(sorted_json_str)
```

이와 같은 방법으로 JSON 데이터를 정렬할 수 있습니다. 다른 프로그래밍 언어에서도 유사한 방법으로 JSON 데이터를 정렬할 수 있으니, 사용하는 언어의 JSON 라이브러리를 참고하세요.
"""
split_messages = split_message(message, MAX_LEN_SLACK)

# 나누어진 메시지 출력
for idx, msg in enumerate(split_messages):
    print(f"파트 {idx + 1}:")
    print(msg)
    print("\n")
