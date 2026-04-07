# Notion Exporter

Notion 페이지를 Markdown으로 내보내는 스크립트입니다. 공식 Notion API를 사용합니다.

## 설치

```bash
pip install -r requirements.txt
```

## 설정

```bash
cp .env.example .env
# .env 파일에 Notion Integration API 토큰 입력
```

Notion Integration 생성: https://www.notion.so/my-integrations

## 실행

```bash
python export.py
```

`build/{NOTION_PAGE_NAME}/` 디렉토리에 Markdown 파일이 생성됩니다.

## References

- https://github.com/indox-ai/notion-exporter
- https://developers.notion.com/
