"""Tool package.

Public API: the registry plumbing plus the shared default_registry.
Tool functions themselves register via side-effect imports of the
sibling submodules below — importing this package is enough for the
agent to see every built-in tool.
"""
from src.tools.registry import (
    ToolContext,
    ToolDef,
    ToolExecutor,
    ToolRegistry,
    default_registry,
    tool,
)

# Side-effect imports: each submodule uses @tool(default_registry, ...) on
# one or more functions. Importing them registers those tools.
# NOTE: 'time' below is src/tools/time.py (the get_current_time tool),
# NOT stdlib time. Do not do `from src.tools import time` expecting
# stdlib — that resolves to our submodule. stdlib time is still safely
# imported as `import time` inside registry.py / slack.py.
from . import (  # noqa: F401  (imported for side effects)
    image,
    memory,
    search,
    slack,
    time,
    web,
)

__all__ = [
    "ToolContext",
    "ToolDef",
    "ToolExecutor",
    "ToolRegistry",
    "default_registry",
    "tool",
]
