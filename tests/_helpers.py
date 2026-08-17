"""Shared test fixtures used across the routing/handlers test suites.

Mirrors the `tests/tools/_helpers.py` pattern. Anything used by more
than one of `test_app.py`, `test_router.py`,
`test_handlers_message.py`, `test_handlers_reactions.py` lives here;
single-file fixtures stay in their owning test file.
"""
from __future__ import annotations


class _FakeCreds:
    """SSM CredentialsStore stand-in for tests that exercise the
    receiver/worker resolution paths."""

    def __init__(self, mapping):
        self._map = mapping
        self.calls = []

    def get(self, app_id):
        self.calls.append(app_id)
        return self._map.get(app_id)


class _FakeDedup:
    """Minimal DedupStore stand-in: reserve always succeeds, no throttle.

    Mirrors the two-stage contract (reserve + mark_done + is_done) so
    handler tests don't have to know about the new completion marker.
    """

    def __init__(self):
        self.done_keys: set[str] = set()

    def reserve(self, key, user="system"):
        return True

    def is_done(self, key):
        return key in self.done_keys

    def mark_done(self, key, user="system"):
        self.done_keys.add(key)

    def count_user_active(self, user):
        return 0


class _NullMetadata:
    """No-op AppMetadataStore for tests that pass api_app_id but don't care
    about the registry side-effect."""

    def record(self, *_args, **_kwargs):
        pass
