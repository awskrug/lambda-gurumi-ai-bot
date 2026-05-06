"""Worker-side event handlers.

Each submodule owns one Slack event family:
- `message` — `app_mention` and DM `message` events (the primary user-facing flow).
- `reactions` — `reaction_added` events; dispatch table driven via `REACTION_HANDLERS`.

`src/router.py::_process_worker` branches on `event["type"]` and dispatches here.
"""
