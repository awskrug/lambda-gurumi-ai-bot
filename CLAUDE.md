# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
cp .env.example .env.local   # fill in values

# Local CLI runner (no Slack connection needed; streaming is default)
python localtest.py "질문"
python localtest.py --no-stream "질문"   # wait for full answer, then print
python localtest.py --quiet-steps "질문" # hide intermediate step logs
python localtest.py                       # interactive stdin (Ctrl+D)

# Tests
python -m pytest
python -m pytest --cov=src --cov-report=term-missing
python -m pytest tests/test_agent.py::test_agent_runs_tool_then_returns_text -v
python -m pytest tests/llms/test_bedrock.py -v                          # LLM provider unit tests
python -m pytest tests/tools/test_web.py -v                             # fetch_webpage + SSRF guard

# Provision per-Slack-app secrets in SSM Parameter Store BEFORE that app
# sends events (the Lambda has no global SLACK_BOT_TOKEN — secrets are
# resolved per request keyed on api_app_id):
aws ssm put-parameter --name /gurumi-bot/apps/A0123ABC/signing_secret \
    --type SecureString --value "$SLACK_SIGNING_SECRET"
aws ssm put-parameter --name /gurumi-bot/apps/A0123ABC/bot_token \
    --type SecureString --value "$SLACK_BOT_TOKEN"

# Deploy (requires IAM OIDC role `lambda-gurumi-bot`)
npm i -g serverless@3
npm i serverless-python-requirements
# export OPENAI_API_KEY / XAI_API_KEY / ... first
serverless deploy --stage dev --region us-east-1
```

Lambda entrypoint: `app.lambda_handler`. Slack events land at `POST /slack/events` via API Gateway.

## Core agent pipeline — DO NOT bypass or shortcut

Every user turn flows through the same four phases, in order:

```
질문 (user message)
  ↓
의도·계획 (intent + plan — one LLM hop; native function calling emits
           tool_calls in the same response when tools are needed)
  ↓
툴 사용 (tool execution — repeats as the LLM keeps calling tools)
  ↓
응답 (compose the final answer once the LLM stops requesting tools)
```

"의도 파악" and "계획" are a single step in code: one call to
`LLMProvider.chat(..., tools=registry.specs())`. The LLM's response
carries both the interpretation of the user request AND the proposed
tool_calls (if any) in one shot. Do NOT split this into a separate
intent-classifier hop — that adds a full LLM roundtrip for no gain
and diverges from native function-calling semantics.

**Design rules — invariants for future changes:**

1. **Intent is always an LLM decision.** Never use keyword heuristics
   (e.g., `"그려"`/`"draw"` → image generator) to bypass the agent.
   The LLM reads the message and emits `tool_calls` to reflect intent.
2. **No phase shortcuts.** Even for "obvious" image requests, we still
   go through the full hop: LLM plan → `generate_image` tool_call → tool
   execution → LLM compose. Skipping the compose step to save seconds
   means the bot can't caption, follow up, or react to tool errors.
3. **Tool orchestration happens inside the agent loop**, not in
   `app.py`. `app.py` wires Slack concerns (placeholder, streaming,
   history). `src/agent.py` owns the loop. Don't push intent
   detection out of the agent.
4. **Slowness is a streaming / infrastructure problem, not a
   pipeline-shortcut problem.** If the loop is slow, fix it with
   async invocation, model choice, or streaming UX — not by
   stripping phases.

## Architecture — the non-obvious parts

### Agent loop uses NATIVE function calling, not JSON prompting

`src/agent.py` passes `registry.specs()` directly to `LLMProvider.chat(tools=...)`. The provider (`src/llms/`) translates that to OpenAI `tools=[{type:"function",function:{...}}]` or Bedrock `tools=[{name, description, input_schema}]` (Claude) / `toolConfig` (Nova). There is **no JSON-in-prompt parsing** — tool calls arrive as structured objects. Loop terminates when `stop_reason != "tool_use"` or `max_steps` hit. On max_steps, a forced compose step (`_compose_without_tools`) runs with `tools=None`.

Duplicate tool-call suppression: `_call_signature` = `name + sha1(args_json)`. A repeated signature within the loop is short-circuited with `{"ok": False, "error": "duplicate call skipped"}` and handed back to the LLM so it can move on.

### Three LLM provider families, one Protocol

`LLMProvider` is a Protocol implemented by `OpenAIProvider`, `XAIProvider`, and `BedrockProvider`. OpenAI and xAI share the OpenAI wire format, so they both extend `_OpenAICompatProvider` and reuse the module-level helpers (`_to_openai_wire_messages`, `_parse_openai_completion`, `_consume_openai_stream`) rather than duplicating stream/tool_calls handling.

- **OpenAIProvider**: default OpenAI endpoint. `_token_params` switches between `max_tokens` (legacy chat) and `max_completion_tokens` (gpt-5 / o1 / o3 / o4 reasoning).
- **XAIProvider**: `base_url="https://api.x.ai/v1"`, explicit `api_key`. Grok chat models accept the legacy `max_tokens + temperature` combo, so we never use `max_completion_tokens` here. Image generation omits `size` (xAI uses `aspect_ratio` / `resolution`) and always requests `response_format="b64_json"` so we can decode bytes locally.
- **BedrockProvider**: routes internally on model family prefix (Bedrock IDs and their `us./eu./apac./global.` inference-profile variants are both accepted):
  - `anthropic.claude*` → `invoke_model` with Messages API shape, `content[].type=="tool_use"` parsing.
  - `amazon.nova*` → `converse` / `converse_stream` with `toolConfig` + `output.message.content[].toolUse`.
  - Unknown → Claude path without tools.

`_to_anthropic_messages` / `_to_nova_messages` translate our canonical role/tool_calls/tool messages to each backend's shape. `tool` role becomes an Anthropic `tool_result` content block inside a user message; Nova becomes a `toolResult` content block.

Image generation is family-routed too: Titan/Nova-Canvas use `TEXT_IMAGE` task; Stability uses `text_prompts`. See `_build_image_body`.

`_CompositeProvider` wraps two providers when text and image providers differ (e.g., OpenAI text + Bedrock image).

### Multi-tenant credential resolution via SSM Parameter Store

This Lambda serves multiple distinct Slack apps from a single deployment. There is no global `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` env var — secrets are looked up per request from SSM at `{SSM_PARAMS_PREFIX}/{api_app_id}/signing_secret` and `.../bot_token` (both `SecureString`). `src/credentials.py::CredentialsStore` does the GetParameters call with a 5-min TTL in-process cache (negative results cached too, so a misconfigured app's burst can't storm SSM). Rotation is reflected within one TTL window; cached `App` instances in `_bolt_apps` carry the secret tuple alongside so a fresh tuple after refresh triggers a rebuild — otherwise rotation wouldn't take effect until the container died.

The receiver-path body parse is done *before* signature verification — but only to extract `api_app_id` for credential lookup. Signature verification still happens against the resolved signing_secret inside Bolt, so a forged `api_app_id` just resolves to the wrong (or no) secret and gets rejected. `url_verification` events (Slack's setup ping when an operator registers Event Subscriptions) are the one exception: the body has no `api_app_id` so we can't pick a secret, and we echo the `challenge` directly without signature check. The body carries no actionable payload, so this break of the chicken-and-egg is safe.

When credentials are missing, the request returns HTTP 200 with a structured `request.unknown_app` log — Slack would otherwise retry an unrecoverable misconfiguration. Operator must populate Parameter Store before that app's events stop being dropped.

`src/app_metadata.py::AppMetadataStore` lazily upserts an `app:{api_app_id}` row into the same DynamoDB table on every successful event (after dedup passes). The row has NO `expire_at` attribute, so the table-level TTL never deletes it — a registry of installed apps materializes automatically. Recording is gated on `if api_app_id:` inside `_process` so the legacy / blank-app code path doesn't pollute the registry.

### Receiver / worker split via Lambda async self-invoke

`lambda_handler` routes one of two ways based on the event shape:

- **Receiver path** (Slack → API Gateway → Lambda): `_route_request` parses the body for `api_app_id`, resolves credentials, gets-or-builds a per-app cached Bolt `App` keyed by that `api_app_id`, and hands the request to Bolt's `SlackRequestHandler`. Bolt then verifies the Slack signature with that app's signing_secret and dispatches to the `app_mention` / `message` handlers. Each handler `ack()`s, then calls `_enqueue_worker(event, is_dm, api_app_id)` which issues a single `lambda:Invoke` against this same function with `InvocationType=Event` and a `{"_worker": True, ..., "api_app_id": "..."}` payload, then returns. The receiver path is back to HTTP 200 within a few hundred ms. That's why Lambda timeout (`serverless.yml: timeout: 300`) can be much larger than the API Gateway REST integration limit (29s) — the receiver never runs long enough to care, and the HTTP response is already home before the worker even starts. Fallback: if `AWS_LAMBDA_FUNCTION_NAME` is unset (local harness) or `lambda.invoke` raises, `_enqueue_worker` runs `_process_worker` inline so the message isn't dropped.
- **Worker path** (Lambda async self-invoke): `lambda_handler` short-circuits on `event["_worker"] is True` and calls `_process_worker`. The worker re-resolves the bot_token from SSM keyed on the carried `api_app_id` (we deliberately do NOT ship tokens through the invoke payload — Lambda invoke payloads can show up in CloudTrail), mints a fresh `WebClient`, and calls `_process(...)`. The full agent run — streaming, tool calls, image generation — happens here, with Lambda's 300s budget all to itself.

### Slack retry → DynamoDB conditional put dedup

Three converging retry sources all funnel through one key: Slack's own 3-attempt retry schedule on the receiver, AWS Lambda's built-in 2x retry on async worker failure, and any accidental re-dispatch. `lambda_handler` short-circuits when `X-Slack-Retry-Num` header is present (returns 200 OK) so Slack retries never spawn a second worker. Inside the worker, the first line of `_process()` is `DedupStore.reserve(f"dedup:{client_msg_id}")` which does `put_item(ConditionExpression="attribute_not_exists(id)")`. Duplicate key raises `ConditionalCheckFailedException` → False → silent return. This is the only race-safe dedup (get-then-put has a window). TTL 1h via `expire_at`. Lambda async worker retries on the same `_worker` payload also hit the same dedup row, so a transient worker failure that Lambda retries can't produce a second reply.

### Single table, three key prefixes

`DYNAMODB_TABLE_NAME` stores three categories of rows:
- `dedup:{msg_id}` — one-shot reservation rows with `expire_at` (1h TTL).
- `ctx:{thread_ts}` — thread conversation memory with `expire_at` (1h TTL).
- `app:{api_app_id}` — app registry rows **without** `expire_at`. DynamoDB TTL only acts on items that explicitly carry the configured attribute, so permanent rows coexist with TTL'd rows in the same table — adding `expire_at` to an `app:` row by mistake would silently auto-evict the registry.

GSI `user-index` (hash `user`, range `expire_at`) backs per-user throttle via `count_user_active(user)`. `ConversationStore.put` trims with `truncate_to_chars(messages, max_chars)` (drop oldest until serialized size fits).

### Message splitting is code-fence-aware

`MessageFormatter.split_message` (in `src/slack_helpers.py`) splits on `\`\`\`` first (so complete code blocks survive), then on `\n\n`, then on `.!?` sentence boundaries, then hard slice. `_merge_small` rejoins adjacent small chunks up to `max_len`. First chunk goes via `chat_update` on the placeholder message; the rest via `chat_postMessage(thread_ts=…)`. If `chat_update` fails (`msg_too_long` etc.), that chunk falls back to a new message.

### Public web fetching is SSRF-gated

`fetch_webpage` uses `_validate_public_https_url` (in `src/tools/web.py`) to enforce `https`, reject IP literals, and drop DNS results resolving to any non-public address (private / loopback / link-local / reserved / multicast / unspecified / non-global — CGNAT `100.64.0.0/10` included). The Jina Reader path (`{JINA_READER_BASE}/{percent-encoded url}`) does the actual network hop against the target; the raw fallback (and only the raw fallback, since the Jina path uses Jina's own fetch) goes direct with a `_NoRedirectHandler` that refuses 3xx, so a redirect into RFC1918 space can't slip past the pre-flight DNS check. Body size is capped by `MAX_WEB_BYTES` on both paths; if Jina exceeds the cap we fall through to raw (the direct fetch may be smaller than Jina's markdown-ified output). Web helpers live in `src/tools/web.py`. Every tool submodule registers itself into `default_registry` at import time; `src/tools/__init__.py` imports the submodules so importing the package is enough to make every built-in tool available.

### Config is lazy, not import-time

`Settings.from_env()` runs at module load but does NOT validate Slack credentials — there are no global Slack secrets to validate in the multi-tenant model. `Settings.slack_bot_token` survives only as a `localtest.py` convenience for exercising Slack-reading tools from the CLI; the Lambda runtime path never reads it.

Enum/int validation quietly falls back to defaults with a warning: invalid `LLM_PROVIDER=mystery` → `openai`, `AGENT_MAX_STEPS=not-int` → `3`, below-minimum values clamp up.

### Streaming runs on every LLM hop

`OpenAIProvider.chat(on_delta=...)` switches into `stream=True` and forwards content deltas as they arrive. When the model starts a `tool_calls` delta (preamble like "Let me search..."), forwarding is suppressed — that pre-tool commentary would leak into the final reply. Tool_calls are accumulated across chunks and returned alongside the content. The agent passes `self.on_stream` into every `chat()` call, so when the LLM decides to answer directly (no tools) the user sees tokens immediately. A separate `stream_chat()` path still exists for the forced compose at `max_steps` and for Bedrock paths that don't yet support tool+stream natively.

Stream throttling is handled inside `StreamingMessage.append()` (`min_interval=0.6s`), not by a wrapper in `app.py`. `StreamingMessage` also rolls into a fresh `chat_postMessage` when the fallback buffer approaches `max_len`, and `stop()` splits an oversized final answer using `MessageFormatter` so no single update hits Slack's `msg_too_long` error.

### Structured logging with request_id

`src/logging_utils.py` installs a JSON handler on root. `set_request_id(uuid)` is called at the start of each `_process`. `log_event(logger, "agent.done", steps=..., tokens_in=...)` emits records whose `extra_fields` dict survives into the JSON payload — useful for CloudWatch Insights queries. Because `logging.LoggerAdapter.process()` in Python 3.12 overwrites `extra=`, `log_event` dispatches via `logger.logger` (the underlying `Logger`) instead of the adapter.

### Extension points

**Add a new tool.** Create `src/tools/<name>.py` with one or more functions decorated by `@tool(default_registry, name="...", description="...", parameters={...})`. Add `<name>` to the side-effect import block in `src/tools/__init__.py`. Add `tests/tools/test_<name>.py`. That's it — the agent loop sees the new tool because `default_registry` is populated at import time.

**Add a new LLM provider.** Create `src/llms/<name>.py` with a class that satisfies the `LLMProvider` Protocol (`chat`, `stream_chat`, `describe_image`, `generate_image`). Add a branch to `src/llms/factory.py`'s `get_llm`, and if the provider introduces new model families extend `_VALID_PROVIDERS` in `src/config.py`. Add `tests/llms/test_<name>.py`.

Neither extension requires editing the registry or the agent loop.

## Deployment

`serverless.yml` provisions:
- Lambda: python3.12, x86_64, 5120MB, 300s timeout. The 300s value only applies to the worker path — the receiver path returns HTTP 200 within a few hundred ms via `_enqueue_worker`'s async self-invoke. The API Gateway REST integration timeout (29s) is only relevant to the receiver, which finishes long before that, so extending the worker budget to 300s (or further, up to Lambda's 900s cap) is safe. Tool timeouts (e.g. `generate_image` 240s) are sized so that compose + upload + history-save fit in the worker's remaining budget. (x86_64 matches the Ubuntu GitHub Actions runner so pip installs wheels — including native ones like `pydantic_core` — that run on the Lambda runtime. Switching to arm64 requires a Docker-based build path via serverless-python-requirements and is deferred.)
- IAM (runtime Lambda role) also grants `lambda:InvokeFunction` on this function's own ARN so `_enqueue_worker` can fire the async self-invoke.
- DynamoDB: hash `id`, GSI `user-index` (user + expire_at, KEYS_ONLY), TTL `expire_at`.
- IAM (runtime Lambda role): `dynamodb:GetItem/PutItem/Query` on table + GSI, `bedrock:InvokeModel*`/`Converse*`.

### GitHub Actions workflows

Three files under `.github/workflows/`:

- `push.yml` — on `push` to `main` (and `workflow_dispatch`). Runs `pytest --cov=src`, sets up Node 20 + Serverless v3, assumes the OIDC role `lambda-gurumi-bot`, then `serverless deploy --stage dev --region us-east-1`.
- `sync-notion.yml`, `sync-awsdocs.yml` — `workflow_dispatch` only (schedule commented out), each gated by `vars.ENABLE_SYNC_NOTION` / `ENABLE_SYNC_AWSDOCS`. Both call `aws cloudformation describe-stacks` expecting outputs `S3Bucket` / `KnowledgeBaseId` / `DataSourceId` that `serverless.yml` does not define, and invoke ingestion scripts (`scripts/notion/export.py`, `scripts/awsdocs/sync.sh`) that have been deleted. **They fail if enabled.** See "Excluded (Phase 2+)".

### OIDC role (`.github/aws-role/`)

Separate from the Lambda runtime role. `trust-policy.json` allows both `repo:awskrug/lambda-gurumi-bot:*` and `repo:nalbam/lambda-gurumi-bot:*`. `role-policy.json` is intentionally wider than current needs — it already grants `s3vectors:*`, `bedrock:*KnowledgeBase*`, `bedrock:*DataSource*`, `bedrock:*Agent*` (scoped to `lambda-gurumi-bot-*`) so Phase-2 KB work can land without IAM changes.

## Testing

206 tests, 84% overall coverage (the drop vs. the 89% of `src/*`-only measurement is just `app.py` now being in scope: its `_process` path is exercised by live Slack traffic, not unit tests). `pytest.ini` pins `testpaths = tests`, `filterwarnings = ignore::DeprecationWarning`. Key approach:

- Tests mirror source layout: `tests/llms/` for each `src/llms/*` submodule, `tests/tools/` for each `src/tools/*` submodule. Top-level `tests/test_agent.py`, `test_config.py`, `test_dedup.py`, `test_logging_utils.py`, `test_slack_helpers.py` cover the non-packaged modules.
- Shared tool-test fixtures (`_ctx`, `_settings`, `_streamed_read`) live in `tests/tools/_helpers.py` — individual test files import from there instead of redefining them.
- `moto[dynamodb]` for `DedupStore` / `ConversationStore` integration tests.
- Network patches target the submodule where `urllib` / `socket` is imported, not the package: `patch("src.tools.slack.urllib.request.urlopen")` for Slack file fetch, `patch("src.tools.search.urllib.request.urlopen")` for `search_web`, `patch("src.tools.web.urllib.request.urlopen")` and `monkeypatch.setattr("src.tools.web.socket.getaddrinfo", …)` for `fetch_webpage`.
- `ScriptedLLM` (in `tests/test_agent.py`) emits predefined `LLMResult` sequences to drive the agent loop without any network.
- Provider tests use `MagicMock` clients — no real OpenAI / Bedrock / xAI calls.
- `tests/test_config.py` builds `Settings` from `monkeypatch`-controlled env without reloading the module.
- `reportlab` (dev-only) synthesizes real PDFs for `read_attached_document` parser coverage.

Per-module coverage:

- `app.py` 35% (routing and worker fan-out covered by `tests/test_app.py`; `_process` is only hit in production)
- `agent.py` 96%, `config.py` 97%, `dedup.py` 80%, `slack_helpers.py` 86%, `logging_utils.py` 97%
- `llms/`: `base.py` 70%, `openai_wire.py` 96%, `openai.py` 100%, `xai.py` 100%, `bedrock.py` 78%, `composite.py` 87%, `factory.py` 94%
- `tools/`: `registry.py` 100%, `slack.py` 87%, `search.py` 93%, `web.py` 97%, `image.py` 100%, `time.py` 100%

## Things that are easy to break

- **Dropping the `_CompositeProvider` branch** in `get_llm` breaks mixed-provider setups (OpenAI text + Bedrock image).
- **Changing `DedupStore.reserve` to a read-then-write pattern** reintroduces the retry race.
- **Losing the `id` prefix scheme** (`dedup:` vs `ctx:`) collides the two store types.
- **Switching to `LoggerAdapter.info(extra=…)`** — in Python 3.12 the adapter's `process()` overwrites `extra`; keep going through `logger.logger` for `extra_fields`.
- **Removing the SSRF host allowlist** (`SLACK_FILE_HOSTS`) shared by `read_attached_images` and `read_attached_document` (`_fetch_slack_file`) opens up arbitrary URL fetch with the bot token.
- **Adding a tool without updating `ToolRegistry.specs()`** — the `@tool` decorator handles both dispatch and LLM schema from a single declaration; inline dict tricks will silently desync.
- **Removing the SSRF guard (`_validate_public_https_url`) on `fetch_webpage`** re-opens fetch to RFC1918 space and cloud-metadata endpoints (e.g. `169.254.169.254`).
- **Enabling redirects on the `fetch_webpage` raw fallback** — a 302 to a private host bypasses the pre-flight DNS check; keep `_NoRedirectHandler` installed.
- **DNS rebinding on `fetch_webpage` raw fallback**. The pre-flight `getaddrinfo` check and the eventual TCP connect are two separate DNS lookups; a TTL=0 attacker can flip between them. Lambda's environment makes the attack hard and impact is bounded (no VPC by default), but don't treat `_validate_public_https_url` as a guarantee that the actual connection hits the same IP. If you ever add VPC/private-subnet egress, revisit this.
- **Dropping the Nova branch in `BedrockProvider.describe_image`**. Nova chat models speak the Converse API with an `image` content block — sending Claude's Messages body at a Nova model ID fails with `ValidationException`. `chat()` already family-routes; the vision entrypoint must do the same.
- **Removing `SlackMentionAgent`'s `finally: self.executor.close()`**. The agent creates its own `ToolExecutor` (and hence a `ThreadPoolExecutor`) unless one is injected. Without the close, every Lambda warm invocation adds new non-daemon workers to the process registry that never unwind until interpreter exit.
- **Narrowing `ToolExecutor.execute`'s exception catch back to a stdlib allowlist**. Provider SDKs raise their own (`openai.APIError`, `anthropic.APIError`, `httpx.HTTPError`) that don't inherit from `ValueError`/`TypeError`; when they escape the executor the whole agent loop aborts instead of handing the failure back to the LLM as `{"ok": False, ...}` for recovery.
- **Applying channel allowlist to DMs**. `_process()` skips `channel_allowed` when `is_dm=True` — DM channel IDs are D-prefixed and not normally in `ALLOWED_CHANNEL_IDS`, so enforcing there would instantly lock out every user's direct-message path the moment an operator set a channel allowlist.
- **Shipping bot tokens through the Lambda invoke payload**. `_enqueue_worker` carries only `api_app_id`; the worker re-fetches secrets from SSM. Lambda invoke payloads can show up in CloudTrail and downstream tooling — never put a `bot_token` or `signing_secret` in the JSON we hand to `lambda:Invoke`.
- **Caching `Bolt App` by `api_app_id` alone** (without the secret tuple as part of the cache value). Rotation in Parameter Store would then never take effect on warm containers — the cached `App` would keep verifying with the old `signing_secret` until the container died. Keep the `((signing_secret, bot_token), App)` shape in `_bolt_apps` so `_get_bolt_app` rebuilds when the tuple changes.
- **Verifying signature on `url_verification`**. The setup-time ping has no `api_app_id` in the body, so we can't pick a `signing_secret` to verify with. The body is just `{type, token, challenge}` with no actionable payload — echo `challenge` directly. Adding signature verification here would require either a global secret (defeats multi-tenant) or trying every known secret (ugly + leaks app-existence information).
- **Writing `expire_at` on `app:` rows**. The DynamoDB table TTL only deletes items that explicitly carry the configured attribute, which is exactly what makes the registry rows persistent. `AppMetadataStore.record` deliberately writes `first_seen_at` + `last_seen_at` + `team_id` and nothing else — adding `expire_at` would make the registry self-evict.
- **Recording app metadata before dedup**. Slack retries and Lambda async retries both arrive with the same `client_msg_id`; if metadata is recorded before `DedupStore.reserve`, every retry bumps `last_seen_at` and inflates apparent activity. Keep the `record(...)` call after the dedup check passes.
- **Removing the negative-result cache in `CredentialsStore`**. A misconfigured app sending a burst of events would otherwise hit SSM `GetParameters` once per request — easy to blow through SSM throttle quotas. Negative results (missing app) are cached for the same TTL window as positive ones for this reason.

## Excluded (Phase 2+)

- **Bedrock Knowledge Base (S3 Vectors + RAG) ingestion pipeline.** Scaffolding exists (IAM policy + `sync-notion.yml` / `sync-awsdocs.yml`), but `serverless.yml` does not provision `S3Bucket` / `KnowledgeBase` / `DataSource`, and the ingestion scripts (`scripts/notion/export.py`, `scripts/awsdocs/sync.sh`) were removed. Both must be restored to re-enable the workflows.
- `reaction_added` event wiring and domain-specific handlers.
- CloudWatch Alarms, X-Ray tracing, languages other than `ko` / `en`.
