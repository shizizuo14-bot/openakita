from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILTIN_MCPS = ROOT / "mcps"


def test_builtin_mcp_python_module_targets_exist() -> None:
    missing: list[str] = []

    for metadata_path in sorted(BUILTIN_MCPS.glob("*/SERVER_METADATA.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        command = str(metadata.get("command") or "")
        args = metadata.get("args") or []

        if not isinstance(args, list):
            continue

        command_name = Path(command).name.lower()
        if command_name not in {"python", "python.exe", "python3", "python3.exe"}:
            continue

        if len(args) < 2 or args[0] != "-m":
            continue

        module_name = args[1]
        if not isinstance(module_name, str) or not module_name.startswith("openakita."):
            continue

        if importlib.util.find_spec(module_name) is None:
            missing.append(f"{metadata_path.relative_to(ROOT)} -> {module_name}")

    assert not missing, "Builtin MCP metadata points at missing Python modules: " + ", ".join(
        missing
    )


def test_bundled_entries_requiring_credentials_are_not_auto_connect() -> None:
    """A bundled entry needing a token must not auto-connect before it is configured."""
    offenders: list[str] = []

    for metadata_path in sorted(BUILTIN_MCPS.glob("*/SERVER_METADATA.json")):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("headers") and metadata.get("autoConnect", False):
            offenders.append(str(metadata_path.relative_to(ROOT)))

    assert not offenders, (
        "These bundled MCP entries declare autoConnect while requiring header "
        "credentials, so they would fail on every start until configured: "
        + ", ".join(offenders)
    )


def test_catalog_headers_only_warn_for_auto_connecting_servers(caplog) -> None:
    """Resolving catalog headers must not warn for servers that are merely listed.

    Regression: unresolved ``${VAR}`` placeholders in SERVER_METADATA.json were
    reported at WARNING while *browsing* the bundled catalog, so an entry the
    user never enabled looked like a fault report.
    """
    import logging

    from openakita.tools.mcp_catalog import _resolve_headers

    raw = {"Authorization": "${SOME_UNSET_TOKEN}"}

    with caplog.at_level(logging.WARNING, logger="openakita.tools.mcp_catalog"):
        resolved = _resolve_headers(raw, {}, warn=False)
    assert resolved == {}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "browsing a non-auto-connect catalog entry must stay quiet"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="openakita.tools.mcp_catalog"):
        _resolve_headers(raw, {}, warn=True)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "a server that intends to connect must still report its missing credentials"
    )
