"""CLI for managing multi-tenant Slack app credentials.

Manages two stores in tandem (the same pair the Lambda runtime resolves
per request):

    SSM Parameter Store SecureString:
        {SSM_PARAMS_PREFIX}/{app_id}/signing_secret
        {SSM_PARAMS_PREFIX}/{app_id}/bot_token

    DynamoDB row:
        id = "app:{app_id}"  with first_seen_at / last_seen_at / team_id
        (TTL-exempt — no `expire_at` column)

The CLI never prints secret values, never accepts secrets as positional
CLI args (those would land in shell history), and requires an explicit
app_id confirmation before destructive deletes. Use `--signing-secret-env`
/ `--bot-token-env` for scripted provisioning; without those flags the
secrets are read interactively via `getpass` so they don't appear on the
terminal or in scrollback.

Usage:
    python scripts/apps.py list
    python scripts/apps.py list --json
    python scripts/apps.py get A0123ABC
    python scripts/apps.py set A0123ABC                          # interactive
    SIG=... TOK=... python scripts/apps.py set A0123ABC \\
        --signing-secret-env=SIG --bot-token-env=TOK             # scripted
    python scripts/apps.py delete A0123ABC                       # confirm + delete both
    python scripts/apps.py delete A0123ABC --yes                 # skip confirm
    python scripts/apps.py delete A0123ABC --keep-metadata       # SSM only

    # Per-app channel/user allowlist overrides
    python scripts/apps.py acl get A0123ABC
    python scripts/apps.py acl set A0123ABC --channels=C1,C2
    python scripts/apps.py acl unset A0123ABC --channels --users

    # Per-app PERSONA_MESSAGE override (string, not list)
    python scripts/apps.py persona get A0123ABC
    python scripts/apps.py persona set A0123ABC "당신은 친근한 어시스턴트"
    python scripts/apps.py persona set A0123ABC --from-file persona.txt
    python scripts/apps.py persona unset A0123ABC

    # Make `list` recognizable
    python scripts/apps.py refresh A0123ABC                  # auth.test → team/bot fields
    python scripts/apps.py name set A0123ABC "Production Bot - Acme"
    python scripts/apps.py name unset A0123ABC

After Slack-side rotation, run `set` against the same app_id — SSM
overwrite is enabled, the cached `App` in the Lambda will rebuild within
one `SSM_CACHE_TTL_SECONDS` window automatically.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError

# Allow `python scripts/apps.py` from the repo root by adding it to sys.path
# so `from src.config import Settings` resolves the same way it does in app.py.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.app_metadata import (  # noqa: E402
    ALLOWED_CHANNEL_IDS_ATTR,
    ALLOWED_USER_IDS_ATTR,
    PERSONA_MESSAGE_ATTR,
    AppMetadataStore,
)
from src.config import Settings  # noqa: E402


_KINDS = ("signing_secret", "bot_token")
_DDB_PREFIX = "app:"


def _parse_id_list(raw: str) -> list[str]:
    """Parse comma-separated IDs. Strips whitespace, drops empties.

    `""` → `[]`. The empty list is a meaningful state — it's an explicit
    "allow all per-app" override that wins over the global env var.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _metadata_store(table) -> AppMetadataStore:
    """Build an AppMetadataStore around an already-resolved boto3 Table.

    `table_name` / `region` are unused once `table=` is injected, so the
    empty strings here are inert."""
    return AppMetadataStore(table_name="", region="", table=table)


def _fetch_bot_token(ssm, prefix: str, app_id: str) -> str | None:
    """Fetch a single bot_token from SSM. Returns None if missing.

    Used by `refresh` / `set --verify` to call Slack `auth.test`. We avoid
    printing the value anywhere — it's passed to `slack_sdk` and discarded."""
    name = f"{prefix}/{app_id}/bot_token"
    try:
        res = ssm.get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    return res.get("Parameter", {}).get("Value")


def _domain_from_url(url: str | None) -> str | None:
    """Extract `acme` from `https://acme.slack.com/`. Returns None when the
    URL is missing, unparseable, or not under `*.slack.com`."""
    if not url:
        return None
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if not host.endswith(".slack.com"):
        return None
    return host.rsplit(".slack.com", 1)[0]


def _call_auth_test(token: str) -> dict | None:
    """Call Slack `auth.test`, then upgrade the `user` field from the bot's
    @handle (Default Name in the App config) to its App Display Name via
    `users.info`. Returns the response dict on success, None on failure
    (with a stderr warning).

    Network failures and Slack errors are non-fatal — the CLI's bigger
    purpose (writing secrets / metadata) should still succeed. The
    `users.info` upgrade requires the bot to have `users:read` scope; if
    it doesn't, the @handle from auth.test stays in `user`.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        client = WebClient(token=token)
        res = client.auth_test()
    except SlackApiError as exc:
        print(f"warning: auth.test failed: {exc.response.get('error', exc)}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"warning: auth.test call failed: {exc}", file=sys.stderr)
        return None
    if not res.get("ok"):
        return None
    data = dict(res.data)

    # auth.test's `user` is the bot's @handle in this workspace (e.g.
    # "brucebot" or workspace-renamed "sreassistant2"). Operators usually
    # recognize the App Display Name (e.g. "Bruce Bot") instead — fetch
    # it via users.info on the bot's user_id. Fail silently when the
    # token lacks `users:read` so the rest of the metadata still lands.
    user_id = data.get("user_id")
    if user_id:
        try:
            info = client.users_info(user=user_id)
        except SlackApiError:
            info = None
        except Exception:  # noqa: BLE001
            info = None
        if info is not None and info.get("ok"):
            user_obj = info.get("user", {}) or {}
            profile = user_obj.get("profile", {}) or {}
            display = (
                profile.get("display_name")
                or user_obj.get("real_name")
                or profile.get("real_name")
            )
            if display:
                data["user"] = display

    return data


def _enrich_from_auth_test(store: AppMetadataStore, app_id: str, info: dict) -> None:
    """Persist team_name / bot_user_name / team_domain from an auth.test
    response. Silent no-op if the response is missing the expected fields."""
    store.update_app_info(
        app_id,
        team_name=info.get("team"),
        bot_user_name=info.get("user"),
        team_domain=_domain_from_url(info.get("url")),
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _format_ts(epoch_seconds: Any) -> str:
    if not epoch_seconds:
        return "-"
    try:
        seconds = int(epoch_seconds)
    except (TypeError, ValueError):
        return str(epoch_seconds)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ssm_param_names(prefix: str, app_id: str) -> tuple[str, str]:
    return f"{prefix}/{app_id}/signing_secret", f"{prefix}/{app_id}/bot_token"


def _ssm_status(entry: dict[str, Any]) -> str:
    """ok / none / partial(...). Used in `list` output."""
    has_sig = "signing_secret" in entry
    has_tok = "bot_token" in entry
    if has_sig and has_tok:
        return "ok"
    if not has_sig and not has_tok:
        return "none"
    if has_sig:
        return "partial(no bot_token)"
    return "partial(no signing_secret)"


def _list_ssm_apps(ssm, prefix: str) -> dict[str, dict[str, Any]]:
    """Enumerate `{prefix}/*/{signing_secret,bot_token}` parameters.

    `describe_parameters` returns metadata only (no decrypted values), which
    is exactly what we want — never retrieve secret material we don't need.
    Returns `{app_id: {"signing_secret": LastModifiedDate, "bot_token": ...}}`.
    Unrelated parameters under the prefix are silently skipped so a future
    sibling key (`/something_else`) can't break the listing.
    """
    apps: dict[str, dict[str, Any]] = {}
    paginator = ssm.get_paginator("describe_parameters")
    pages = paginator.paginate(
        ParameterFilters=[
            {"Key": "Name", "Option": "BeginsWith", "Values": [f"{prefix}/"]},
        ]
    )
    for page in pages:
        for p in page.get("Parameters", []):
            name = p["Name"]
            rest = name[len(prefix) + 1 :]  # strip "{prefix}/"
            parts = rest.split("/", 1)
            if len(parts) != 2:
                continue
            app_id, kind = parts
            if kind not in _KINDS:
                continue
            apps.setdefault(app_id, {})[kind] = p.get("LastModifiedDate")
    return apps


def _list_metadata_apps(table) -> dict[str, dict[str, Any]]:
    """Scan for `app:` rows. Single-table design — dedup/ctx rows are filtered out."""
    apps: dict[str, dict[str, Any]] = {}
    kwargs = dict(
        FilterExpression="begins_with(#id, :prefix)",
        ExpressionAttributeNames={"#id": "id"},
        ExpressionAttributeValues={":prefix": _DDB_PREFIX},
    )
    while True:
        res = table.scan(**kwargs)
        for item in res.get("Items", []):
            app_id = item["id"][len(_DDB_PREFIX) :]
            apps[app_id] = item
        if "LastEvaluatedKey" not in res:
            break
        kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]
    return apps


def _visual_width(s: str) -> int:
    """Terminal column count for a string. East-Asian wide / fullwidth chars
    (Hangul, CJK ideographs, fullwidth ASCII) take 2 columns; everything else
    takes 1. `len()` would undercount these, which is why a row containing
    `"당근"` would visually overflow its computed cell and shift later columns
    rightward. Stdlib only — no extra dep."""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """Right-pad `s` with spaces until its *visual* width hits `width`.
    `f"{s:<N}"` would pad to N *characters*, which is wrong for CJK."""
    deficit = width - _visual_width(s)
    return s + (" " * deficit if deficit > 0 else "")


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print an aligned table to current sys.stdout.

    We deliberately do NOT bind to sys.stdout at definition time —
    pytest's capsys swaps sys.stdout per test, and a stale reference
    would route output past the capture, breaking output-asserting tests.
    """
    widths = [_visual_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visual_width(str(cell)))
    sep = "  "
    print(sep.join(_pad(h, w) for h, w in zip(headers, widths)))
    print(sep.join("-" * w for w in widths))
    for row in rows:
        print(sep.join(_pad(str(c), w) for c, w in zip(row, widths)))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_list(args, *, ssm, table, prefix: str, settings=None) -> int:
    ssm_apps = _list_ssm_apps(ssm, prefix)
    md_apps = _list_metadata_apps(table)

    all_ids = sorted(set(ssm_apps) | set(md_apps))
    if not all_ids:
        print("(no apps registered)")
        return 0

    rows = []
    for app_id in all_ids:
        ssm_entry = ssm_apps.get(app_id, {})
        md = md_apps.get(app_id, {})
        rows.append(
            [
                app_id,
                _ssm_status(ssm_entry),
                "ok" if md else "none",
                _resolve_name(md),
                _format_ts(md.get("last_seen_at")),
            ]
        )

    if args.json:
        keys = ["app_id", "ssm", "metadata", "name", "last_seen"]
        print(json.dumps([dict(zip(keys, r)) for r in rows], ensure_ascii=False))
        return 0

    _print_table(["APP_ID", "SSM", "METADATA", "NAME", "LAST_SEEN"], rows)
    return 0


def cmd_get(args, *, ssm, table, prefix: str, settings=None) -> int:
    app_id = args.app_id
    sig_name, tok_name = _ssm_param_names(prefix, app_id)

    ssm_info: dict[str, dict[str, Any]] = {}
    for kind, name in [("signing_secret", sig_name), ("bot_token", tok_name)]:
        try:
            res = ssm.describe_parameters(
                ParameterFilters=[{"Key": "Name", "Values": [name]}]
            )
        except ClientError as exc:
            ssm_info[kind] = {"present": False, "error": str(exc)}
            continue
        params = res.get("Parameters", [])
        if not params:
            ssm_info[kind] = {"present": False, "name": name}
            continue
        p = params[0]
        ssm_info[kind] = {
            "present": True,
            "name": name,
            "last_modified": p.get("LastModifiedDate"),
            "version": p.get("Version"),
        }

    md_item: dict[str, Any] | None
    try:
        res = table.get_item(Key={"id": f"{_DDB_PREFIX}{app_id}"})
        md_item = res.get("Item")
    except ClientError as exc:
        print(f"DynamoDB error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"app_id": app_id, "ssm": ssm_info, "metadata": md_item}, default=str))
        return 0

    print(f"app_id: {app_id}")
    print()
    print("SSM Parameter Store:")
    for kind in _KINDS:
        info = ssm_info[kind]
        if info["present"]:
            print(f"  {kind:<16} present  v{info.get('version')}  last_modified={info.get('last_modified')}")
            print(f"    name: {info['name']}")
        else:
            print(f"  {kind:<16} MISSING")
            print(f"    set with: scripts/apps.py set {app_id}")
    print()
    print("DynamoDB metadata:")
    if md_item:
        print(f"  display_name:   {md_item.get('display_name', '-')}")
        print(f"  team_name:      {md_item.get('team_name', '-')}")
        print(f"  team_domain:    {md_item.get('team_domain', '-')}")
        print(f"  bot_user_name:  {md_item.get('bot_user_name', '-')}")
        print(f"  team_id:        {md_item.get('team_id', '-')}")
        print(f"  first_seen_at:  {_format_ts(md_item.get('first_seen_at'))}")
        print(f"  last_seen_at:   {_format_ts(md_item.get('last_seen_at'))}")
    else:
        print("  (no row — this app hasn't sent an event yet)")
    return 0


def _read_secret(name: str, env_var: str | None, prompter: Callable[[str], str]) -> str | None:
    """Read a secret from the named env var, or interactively via the prompter.

    Empty input means "leave this field unchanged" — that lets `set` rotate
    just one of the two without touching the other.
    """
    if env_var:
        value = os.environ.get(env_var, "")
        if not value:
            print(
                f"warning: env var {env_var} is empty or unset — skipping {name}",
                file=sys.stderr,
            )
            return None
        return value
    value = prompter(f"{name} (input hidden, blank to skip): ")
    return value or None


def cmd_set(args, *, ssm, table, prefix: str, settings=None) -> int:
    app_id = args.app_id
    sig_name, tok_name = _ssm_param_names(prefix, app_id)

    prompter = getattr(args, "_prompter", getpass.getpass)
    signing_secret = _read_secret("signing_secret", args.signing_secret_env, prompter)
    bot_token = _read_secret("bot_token", args.bot_token_env, prompter)

    if not signing_secret and not bot_token:
        print("nothing to update (both fields blank/skipped)", file=sys.stderr)
        return 1

    if signing_secret:
        ssm.put_parameter(Name=sig_name, Value=signing_secret, Type="SecureString", Overwrite=True)
        print(f"updated SSM SecureString: {sig_name}")
    if bot_token:
        ssm.put_parameter(Name=tok_name, Value=bot_token, Type="SecureString", Overwrite=True)
        print(f"updated SSM SecureString: {tok_name}")

    # Auto-verify the new bot_token by calling Slack auth.test, then
    # populate `team_name` / `bot_user_name` / `team_domain` so `apps list`
    # can render a recognizable label. `--no-verify` skips when network /
    # Slack reachability isn't available.
    if bot_token and not getattr(args, "no_verify", False):
        info = _call_auth_test(bot_token)
        if info is not None:
            store = _metadata_store(table)
            _enrich_from_auth_test(store, app_id, info)
            print(
                f"verified token: team={info.get('team')!r} "
                f"bot={info.get('user')!r} "
                f"domain={_domain_from_url(info.get('url'))!r}"
            )
    return 0


def cmd_delete(args, *, ssm, table, prefix: str, settings=None) -> int:
    app_id = args.app_id
    sig_name, tok_name = _ssm_param_names(prefix, app_id)

    targets = []
    if not args.keep_secrets:
        targets.append("SSM signing_secret + bot_token")
    if not args.keep_metadata:
        targets.append("DynamoDB metadata row")
    if not targets:
        print("nothing to delete (both --keep-secrets and --keep-metadata set)", file=sys.stderr)
        return 1

    if not args.yes:
        confirmer = getattr(args, "_confirmer", input)
        prompt = (
            f"DELETE {' + '.join(targets)} for app_id={app_id}?\n"
            f"Re-type the app_id to confirm: "
        )
        confirm = confirmer(prompt)
        if confirm != app_id:
            print("aborted (confirmation didn't match)", file=sys.stderr)
            return 1

    if not args.keep_secrets:
        try:
            res = ssm.delete_parameters(Names=[sig_name, tok_name])
            for n in res.get("DeletedParameters", []) or []:
                print(f"deleted SSM parameter: {n}")
            for n in res.get("InvalidParameters", []) or []:
                print(f"SSM parameter not found (already gone?): {n}")
        except ClientError as exc:
            print(f"SSM delete failed: {exc}", file=sys.stderr)
            return 1

    if not args.keep_metadata:
        try:
            table.delete_item(Key={"id": f"{_DDB_PREFIX}{app_id}"})
            print(f"deleted DynamoDB row: {_DDB_PREFIX}{app_id}")
        except ClientError as exc:
            print(f"DynamoDB delete failed: {exc}", file=sys.stderr)
            return 1
    return 0


# --------------------------------------------------------------------------- #
# acl — per-app channel/user allowlist overrides
# --------------------------------------------------------------------------- #


def _format_acl_field(label: str, per_app: list[str] | None, global_list: list[str]) -> list[str]:
    """Render the per_app / global / effective triple for one ACL field.

    Returns a list of lines (stdout-ready) so callers can interleave with
    other output. The three-state contract — per_app=None means "use global"
    — is shown explicitly so the operator can tell at a glance whether they
    set the override or are inheriting it.
    """
    out = [f"{label}:"]
    if per_app is None:
        out.append("  per-app:    (not set — falls back to global)")
        effective = global_list
    elif per_app == []:
        out.append("  per-app:    [] (explicit ALLOW ALL — overrides global)")
        effective = per_app
    else:
        out.append(f"  per-app:    {per_app}")
        effective = per_app
    out.append(f"  global:     {global_list if global_list else '[] (allow all)'}")
    if effective:
        out.append(f"  effective:  {effective}")
    else:
        out.append("  effective:  [] (allow all)")
    return out


def cmd_acl_get(args, *, ssm, table, prefix: str, settings=None) -> int:
    if settings is None:
        # Defensive — main() always injects settings, but tests may not.
        # Falls back to empty global lists; the per-app side still works.
        settings = Settings.from_env()
    app_id = args.app_id
    store = _metadata_store(table)
    row = store.get(app_id)

    def _resolve(attr: str) -> list[str] | None:
        if row is None or attr not in row:
            return None
        return list(row[attr])

    ch_per = _resolve(ALLOWED_CHANNEL_IDS_ATTR)
    us_per = _resolve(ALLOWED_USER_IDS_ATTR)
    ch_global = list(settings.allowed_channel_ids)
    us_global = list(settings.allowed_user_ids)

    if args.json:
        out = {
            "app_id": app_id,
            "channels": {
                "per_app": ch_per,
                "global": ch_global,
                "effective": ch_per if ch_per is not None else ch_global,
            },
            "users": {
                "per_app": us_per,
                "global": us_global,
                "effective": us_per if us_per is not None else us_global,
            },
        }
        print(json.dumps(out))
        return 0

    print(f"app_id: {app_id}")
    print()
    for line in _format_acl_field("channels", ch_per, ch_global):
        print(line)
    print()
    for line in _format_acl_field("users", us_per, us_global):
        print(line)
    return 0


def cmd_acl_set(args, *, ssm, table, prefix: str, settings=None) -> int:
    if args.channels is None and args.users is None:
        print("nothing to set (specify --channels and/or --users)", file=sys.stderr)
        return 1

    store = _metadata_store(table)
    app_id = args.app_id

    if args.channels is not None:
        values = _parse_id_list(args.channels)
        store.set_allowlist(app_id, ALLOWED_CHANNEL_IDS_ATTR, values)
        if values:
            print(f"set {ALLOWED_CHANNEL_IDS_ATTR} = {values}")
        else:
            print(f"set {ALLOWED_CHANNEL_IDS_ATTR} = [] (explicit allow all per-app)")

    if args.users is not None:
        values = _parse_id_list(args.users)
        store.set_allowlist(app_id, ALLOWED_USER_IDS_ATTR, values)
        if values:
            print(f"set {ALLOWED_USER_IDS_ATTR} = {values}")
        else:
            print(f"set {ALLOWED_USER_IDS_ATTR} = [] (explicit allow all per-app)")
    return 0


def cmd_acl_unset(args, *, ssm, table, prefix: str, settings=None) -> int:
    if not args.channels and not args.users:
        print("nothing to unset (specify --channels and/or --users)", file=sys.stderr)
        return 1

    store = _metadata_store(table)
    app_id = args.app_id

    if args.channels:
        store.unset_allowlist(app_id, ALLOWED_CHANNEL_IDS_ATTR)
        print(f"unset {ALLOWED_CHANNEL_IDS_ATTR} (reverts to global env var)")
    if args.users:
        store.unset_allowlist(app_id, ALLOWED_USER_IDS_ATTR)
        print(f"unset {ALLOWED_USER_IDS_ATTR} (reverts to global env var)")
    return 0


# --------------------------------------------------------------------------- #
# persona — per-app PERSONA_MESSAGE override (string, distinct from ACL)
# --------------------------------------------------------------------------- #


def _abbrev(text: str, limit: int = 200) -> str:
    """Single-line preview of a possibly multi-line persona for `persona get`.
    Newlines collapse to spaces; trailing chars cut with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def cmd_persona_get(args, *, ssm, table, prefix: str, settings=None) -> int:
    if settings is None:
        settings = Settings.from_env()
    app_id = args.app_id
    store = _metadata_store(table)
    row = store.get(app_id)

    if row is None or PERSONA_MESSAGE_ATTR not in row:
        per_app = None
    else:
        per_app = str(row[PERSONA_MESSAGE_ATTR])
    global_value = settings.persona_message or ""
    effective = per_app if per_app is not None else global_value

    if args.json:
        print(json.dumps({
            "app_id": app_id,
            "per_app": per_app,
            "global": global_value,
            "effective": effective,
        }, ensure_ascii=False))
        return 0

    print(f"app_id: {app_id}")
    print()
    print("persona:")
    if per_app is None:
        print("  per-app:    (not set — falls back to global)")
    elif per_app == "":
        print('  per-app:    "" (explicit NO PERSONA — overrides global)')
    else:
        print(f"  per-app:    {_abbrev(per_app)}")
    if global_value:
        print(f"  global:     {_abbrev(global_value)}")
    else:
        print("  global:     (not set)")
    if effective:
        print(f"  effective:  {_abbrev(effective)}")
    else:
        print("  effective:  (no persona)")
    return 0


def _read_persona_value(args) -> str | None:
    """Resolve the persona text from one of the supported input modes.
    Returns None if no input source provided (caller should error)."""
    if getattr(args, "from_stdin", False):
        return sys.stdin.read()
    path = getattr(args, "from_file", None)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    if args.value is not None:
        return args.value
    return None


def cmd_persona_set(args, *, ssm, table, prefix: str, settings=None) -> int:
    value = _read_persona_value(args)
    if value is None:
        print(
            "no input source — pass a positional value, --from-file PATH, or --from-stdin",
            file=sys.stderr,
        )
        return 1

    store = _metadata_store(table)
    store.set_persona(args.app_id, value)
    if value == "":
        print(f"set {PERSONA_MESSAGE_ATTR} = \"\" (explicit no-persona override)")
    else:
        print(f"set {PERSONA_MESSAGE_ATTR} ({len(value)} chars)")
        print(f"  preview: {_abbrev(value)}")
    return 0


def cmd_persona_unset(args, *, ssm, table, prefix: str, settings=None) -> int:
    store = _metadata_store(table)
    store.unset_persona(args.app_id)
    print(f"unset {PERSONA_MESSAGE_ATTR} (reverts to global env var)")
    return 0


# --------------------------------------------------------------------------- #
# refresh / name — make `list` output recognizable
# --------------------------------------------------------------------------- #


def cmd_refresh(args, *, ssm, table, prefix: str, settings=None) -> int:
    """Re-run Slack `auth.test` and update team_name / bot_user_name /
    team_domain. Use after a Slack-side workspace rename or as a backfill
    for apps provisioned before this command existed."""
    app_id = args.app_id
    token = _fetch_bot_token(ssm, prefix, app_id)
    if not token:
        print(f"no bot_token in SSM for {app_id}", file=sys.stderr)
        return 1
    info = _call_auth_test(token)
    if info is None:
        return 1
    store = _metadata_store(table)
    _enrich_from_auth_test(store, app_id, info)
    print(
        f"refreshed {app_id}: team={info.get('team')!r} "
        f"bot={info.get('user')!r} "
        f"domain={_domain_from_url(info.get('url'))!r}"
    )
    return 0


def cmd_name_set(args, *, ssm, table, prefix: str, settings=None) -> int:
    store = _metadata_store(table)
    store.set_display_name(args.app_id, args.name)
    print(f"set display_name for {args.app_id}: {args.name!r}")
    return 0


def cmd_name_unset(args, *, ssm, table, prefix: str, settings=None) -> int:
    store = _metadata_store(table)
    store.unset_display_name(args.app_id)
    print(f"unset display_name for {args.app_id} (auto-populated team/bot fields will fill in)")
    return 0


def _resolve_name(row: dict | None) -> str:
    """Pick the most useful single-line label for an app.

    Priority (most operator-meaningful first):
      1. `display_name` — operator-set explicit alias
      2. `"team_name / bot_user_name"` — both auto-populated from auth.test
      3. `team_name` alone — partial auto-populate
      4. `team_id` alone — only known from event payload, no auth.test yet
      5. `"-"` — nothing known yet
    """
    if not row:
        return "-"
    display = row.get("display_name")
    if display:
        return str(display)
    team_name = row.get("team_name")
    bot_user = row.get("bot_user_name")
    if team_name and bot_user:
        return f"{team_name} / {bot_user}"
    if team_name:
        return str(team_name)
    team_id = row.get("team_id")
    if team_id:
        return str(team_id)
    return "-"


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region", default=None, help="AWS region (default: settings.aws_region)")
    parser.add_argument("--prefix", default=None, help="SSM parameter prefix (default: settings.ssm_params_prefix)")
    parser.add_argument("--table", default=None, help="DynamoDB table (default: settings.dynamodb_table_name)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List all apps known to either store")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_get = sub.add_parser("get", help="Show one app's status")
    p_get.add_argument("app_id")
    p_get.add_argument("--json", action="store_true")
    p_get.set_defaults(func=cmd_get)

    p_set = sub.add_parser(
        "set",
        help="Create/update SSM SecureString secrets (interactive by default)",
    )
    p_set.add_argument("app_id")
    p_set.add_argument(
        "--signing-secret-env",
        default=None,
        help="Read signing_secret from this env var instead of prompting",
    )
    p_set.add_argument(
        "--bot-token-env",
        default=None,
        help="Read bot_token from this env var instead of prompting",
    )
    p_set.add_argument(
        "--no-verify",
        action="store_true",
        dest="no_verify",
        help="Skip the post-write Slack auth.test call (use when offline / Slack unreachable)",
    )
    p_set.set_defaults(func=cmd_set)

    p_del = sub.add_parser("delete", help="Delete app secrets and/or metadata")
    p_del.add_argument("app_id")
    p_del.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    p_del.add_argument("--keep-secrets", action="store_true", help="Don't delete SSM secrets")
    p_del.add_argument("--keep-metadata", action="store_true", help="Don't delete DynamoDB row")
    p_del.set_defaults(func=cmd_delete)

    # `acl` is a nested subcommand group: `acl get`, `acl set`, `acl unset`.
    # The three-state contract is reflected in the flag shapes:
    #   set --channels=<csv|""> writes the attribute (empty = explicit [])
    #   unset --channels         removes the attribute (revert to global)
    p_acl = sub.add_parser(
        "acl",
        help="Manage per-app channel/user allowlist overrides (DynamoDB)",
    )
    acl_sub = p_acl.add_subparsers(dest="acl_cmd", required=True)

    p_acl_get = acl_sub.add_parser("get", help="Show per-app ACL alongside the global env-var fallback")
    p_acl_get.add_argument("app_id")
    p_acl_get.add_argument("--json", action="store_true")
    p_acl_get.set_defaults(func=cmd_acl_get)

    p_acl_set = acl_sub.add_parser(
        "set",
        help="Set per-app allowlist override (empty value = explicit allow all)",
    )
    p_acl_set.add_argument("app_id")
    p_acl_set.add_argument(
        "--channels",
        default=None,
        help='Comma-separated channel IDs, e.g. "C123,C456". Pass "" for explicit empty (overrides global).',
    )
    p_acl_set.add_argument(
        "--users",
        default=None,
        help='Comma-separated user IDs. Pass "" for explicit empty (overrides global).',
    )
    p_acl_set.set_defaults(func=cmd_acl_set)

    p_acl_unset = acl_sub.add_parser(
        "unset",
        help="Remove per-app override; behavior reverts to the global env var",
    )
    p_acl_unset.add_argument("app_id")
    p_acl_unset.add_argument("--channels", action="store_true", help="Remove allowed_channel_ids")
    p_acl_unset.add_argument("--users", action="store_true", help="Remove allowed_user_ids")
    p_acl_unset.set_defaults(func=cmd_acl_unset)

    # `persona` is a separate group from `acl` because PERSONA_MESSAGE is a
    # string (not a list) and goes through a different DynamoDB attribute.
    # The set/unset/get split mirrors `acl` so operators only learn one
    # mental model.
    p_persona = sub.add_parser(
        "persona",
        help="Manage per-app PERSONA_MESSAGE override (DynamoDB)",
    )
    persona_sub = p_persona.add_subparsers(dest="persona_cmd", required=True)

    p_persona_get = persona_sub.add_parser("get", help="Show per-app persona alongside the global env-var fallback")
    p_persona_get.add_argument("app_id")
    p_persona_get.add_argument("--json", action="store_true")
    p_persona_get.set_defaults(func=cmd_persona_get)

    p_persona_set = persona_sub.add_parser(
        "set",
        help='Set per-app persona override. Empty string ("") = explicit no-persona override.',
    )
    p_persona_set.add_argument("app_id")
    p_persona_set.add_argument(
        "value",
        nargs="?",
        default=None,
        help="Persona text. Use --from-file or --from-stdin for multi-line.",
    )
    p_persona_set.add_argument("--from-file", default=None, dest="from_file", help="Read persona text from a file")
    p_persona_set.add_argument("--from-stdin", action="store_true", dest="from_stdin", help="Read persona text from stdin")
    p_persona_set.set_defaults(func=cmd_persona_set)

    p_persona_unset = persona_sub.add_parser(
        "unset",
        help="Remove per-app persona override; behavior reverts to the global PERSONA_MESSAGE env var",
    )
    p_persona_unset.add_argument("app_id")
    p_persona_unset.set_defaults(func=cmd_persona_unset)

    p_refresh = sub.add_parser(
        "refresh",
        help="Re-call Slack auth.test for an app and update team/bot identification fields",
    )
    p_refresh.add_argument("app_id")
    p_refresh.set_defaults(func=cmd_refresh)

    # `name` is a single-attribute group (operator-set display label),
    # parallel to `acl` and `persona` for visual consistency.
    p_name = sub.add_parser(
        "name",
        help="Operator-set display label that takes precedence over auto-populated team/bot info",
    )
    name_sub = p_name.add_subparsers(dest="name_cmd", required=True)
    p_name_set = name_sub.add_parser("set", help="Set a custom display name")
    p_name_set.add_argument("app_id")
    p_name_set.add_argument("name")
    p_name_set.set_defaults(func=cmd_name_set)
    p_name_unset = name_sub.add_parser("unset", help="Remove the custom display name")
    p_name_unset.add_argument("app_id")
    p_name_unset.set_defaults(func=cmd_name_unset)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    region = args.region or settings.aws_region
    prefix = (args.prefix or settings.ssm_params_prefix).rstrip("/")
    table_name = args.table or settings.dynamodb_table_name

    ssm = boto3.client("ssm", region_name=region)
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    return args.func(args, ssm=ssm, table=table, prefix=prefix, settings=settings)


if __name__ == "__main__":
    sys.exit(main())
