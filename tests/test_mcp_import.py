"""Smoke test: importing the MCP server must not load config / credentials.

This guards the contract that config / keys / db are lazily initialized
inside `main()`, so a productized agent runtime (e.g. agentsia-core's
`AgentContext.activate()`) can chdir into an agent's working directory
and set env vars BEFORE the engine reads anything from disk.

If this ever fails, the downstream agent runtime breaks — a fresh import
would search for `config.yaml` in whatever cwd happens to be active at
import time, which is rarely correct.
"""

from __future__ import annotations


def test_mcp_server_import_does_not_read_config(monkeypatch) -> None:
    """Force load_config / load_api_keys to raise if they somehow run at
    import time. A bare `import leadgen.mcp_server.server` must not
    trigger either function."""
    called = {"load_config": 0, "load_api_keys": 0}

    def boom_config(*_a, **_kw):
        called["load_config"] += 1
        raise AssertionError("load_config must NOT run at module import")

    def boom_keys(*_a, **_kw):
        called["load_api_keys"] += 1
        raise AssertionError("load_api_keys must NOT run at module import")

    import leadgen.config.loader as loader_mod

    monkeypatch.setattr(loader_mod, "load_config", boom_config)
    monkeypatch.setattr(loader_mod, "load_api_keys", boom_keys)

    import importlib

    import leadgen.mcp_server.server as server_mod

    importlib.reload(server_mod)

    assert called["load_config"] == 0
    assert called["load_api_keys"] == 0


def test_mcp_server_module_globals_are_none_at_import() -> None:
    """Fresh import leaves `config`, `keys`, `db` as None sentinels.
    main() is the only place that should assign them."""
    import importlib

    import leadgen.mcp_server.server as server_mod

    importlib.reload(server_mod)
    assert server_mod.config is None
    assert server_mod.keys is None
    assert server_mod.db is None


def test_mcp_server_pluggable_class_defaults_are_engine_base() -> None:
    """At import time SCORER_CLASS / DRAFTER_CLASS must point at the
    generic engine classes. Downstream agents override these by passing
    kwargs to main(); the defaults are what a bare `leadgen mcp` uses."""
    import importlib

    import leadgen.mcp_server.server as server_mod

    importlib.reload(server_mod)

    from leadgen.ai.drafter import OutreachDrafter
    from leadgen.ai.scorer import LeadScorer

    assert server_mod.SCORER_CLASS is LeadScorer
    assert server_mod.DRAFTER_CLASS is OutreachDrafter
