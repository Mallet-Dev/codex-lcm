"""codex-lcm command-line interface."""

from __future__ import annotations

import argparse
import ast
import copy
from datetime import datetime
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[import-not-found]

from . import __version__
from .config import LCMConfig
from .db import LCMDatabase
from .worker import run_worker

_PKG = "codex_lcm"
_MCP_SECTION = "[mcp_servers.codex-lcm]"
_MCP_MARKER = "# codex-lcm — lossless context management MCP server"


# --- helpers ---------------------------------------------------------------


def _python_cmd() -> str:
    """The shell command prefix that runs codex-lcm modules with this python."""
    return shlex.quote(sys.executable)


def _backup(path: Path) -> Optional[Path]:
    """Create a timestamped backup of a file before modifying it."""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.bak-codex-lcm-{stamp}")
    shutil.copy2(path, backup)
    return backup


def _write_text(path: Path, content: str) -> None:
    """Atomically replace a text file while preserving its existing mode."""
    write_path = path.resolve(strict=True) if path.is_symlink() else path
    mode = write_path.stat().st_mode & 0o777 if write_path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{write_path.name}.codex-lcm-", dir=write_path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, write_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _build_hooks(python: str) -> dict[str, list[dict[str, Any]]]:
    """The codex-lcm hook definitions keyed by hook event."""
    return {
        "SessionStart": [
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python} -m {_PKG}.hooks.session_start",
                        "timeout": 15,
                        "additionalContextLimit": 8000,
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {"type": "command", "command": f"{python} -m {_PKG}.hooks.user_prompt_submit", "timeout": 10}
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": f"{python} -m {_PKG}.hooks.post_tool_use", "timeout": 10}
                ],
            }
        ],
        "PreCompact": [
            {
                "matcher": "manual|auto",
                "hooks": [{"type": "command", "command": f"{python} -m {_PKG}.hooks.pre_compact", "timeout": 5}],
            }
        ],
        "PostCompact": [
            {
                "matcher": "manual|auto",
                "hooks": [{"type": "command", "command": f"{python} -m {_PKG}.hooks.post_compact", "timeout": 10}],
            }
        ],
        "Stop": [
            {
                "hooks": [{"type": "command", "command": f"{python} -m {_PKG}.hooks.stop", "timeout": 15}],
            }
        ],
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} must contain valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _merge_hooks(existing: dict[str, Any], ours: dict[str, list[dict]]) -> tuple[dict[str, Any], bool]:
    """Merge exact codex-lcm hooks into a copy of `existing`."""
    merged = _strip_codex_lcm_hooks(existing)
    hooks = merged.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks.json field 'hooks' must be a JSON object")

    for event, groups in ours.items():
        event_hooks = hooks.setdefault(event, [])
        if not isinstance(event_hooks, list):
            raise ValueError(f"hooks.json field 'hooks.{event}' must be a JSON array")
        event_hooks.extend(copy.deepcopy(groups))

    merged["hooks"] = hooks
    return merged, merged != existing


def _hook_is_codex_lcm(hook: Any) -> bool:
    if not isinstance(hook, dict):
        return False
    try:
        command = shlex.split(str(hook.get("command", "")))
    except ValueError:
        return False
    for index, part in enumerate(command[:-1]):
        if part == "-m" and command[index + 1].startswith(f"{_PKG}.hooks."):
            return True
    return False


def _group_is_codex_lcm(group: Any) -> bool:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return False
    return any(_hook_is_codex_lcm(hook) for hook in group["hooks"])


def _strip_codex_lcm_groups(groups: list[Any]) -> list[Any]:
    """Remove only codex-lcm hook entries while preserving foreign group members."""
    cleaned: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            cleaned.append(copy.deepcopy(group))
            continue
        kept_hooks = [hook for hook in group["hooks"] if not _hook_is_codex_lcm(hook)]
        if len(kept_hooks) == len(group["hooks"]):
            cleaned.append(copy.deepcopy(group))
            continue
        if kept_hooks or any(key not in {"hooks", "matcher"} for key in group):
            kept_group = copy.deepcopy(group)
            kept_group["hooks"] = kept_hooks
            cleaned.append(kept_group)
    return cleaned


def _strip_codex_lcm_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    cleaned_config = copy.deepcopy(existing)
    hooks = cleaned_config.get("hooks", {})
    if not isinstance(hooks, dict):
        return cleaned_config
    cleaned: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned[event] = groups
            continue
        kept = _strip_codex_lcm_groups(groups)
        if kept or kept == groups:
            cleaned[event] = kept
    cleaned_config["hooks"] = cleaned
    return cleaned_config


def _mcp_block() -> str:
    return (
        "\n"
        f"{_MCP_MARKER}\n"
        f"{_MCP_SECTION}\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [\"-m\", \"{_PKG}.mcp_server\"]\n"
    )


def _strip_toml_comment(line: str) -> str:
    out: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for char in line:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if quote is not None:
            out.append(char)
            if quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "#":
            break
        out.append(char)
        if char in {'"', "'"}:
            quote = char
    return "".join(out)


def _section_header(line: str) -> str:
    return _strip_toml_comment(line).strip()


def _validate_toml_config(text: str) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"config.toml is not valid TOML: {exc}") from exc
    mcp_servers = parsed.get("mcp_servers")
    if mcp_servers is not None and not isinstance(mcp_servers, dict):
        raise ValueError("config.toml mcp_servers must be a table")
    target = mcp_servers.get("codex-lcm") if isinstance(mcp_servers, dict) else None
    if target is not None and not isinstance(target, dict):
        raise ValueError("config.toml codex-lcm MCP server must be a table")
    return parsed


def _toml_literal(value: str) -> Any:
    uncommented = [_strip_toml_comment(line) for line in value.splitlines()]
    try:
        return ast.literal_eval("\n".join(uncommented).strip())
    except (SyntaxError, ValueError):
        return None


def _toml_key_name(raw: str) -> Optional[str]:
    key = raw.strip()
    if not key:
        return None
    if key[0] in {'"', "'"}:
        parsed = _toml_literal(key)
        return parsed if isinstance(parsed, str) else None
    return key


def _toml_key_path(raw: str) -> Optional[tuple[str, ...]]:
    key = raw.strip()
    if not key:
        return None
    parts: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for char in key:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if quote is not None:
            current.append(char)
            if quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            current.append(char)
            quote = char
        elif char == ".":
            part = _toml_key_name("".join(current))
            if part is None:
                return None
            parts.append(part)
            current = []
        else:
            current.append(char)
    if quote is not None:
        return None
    part = _toml_key_name("".join(current))
    if part is None:
        return None
    parts.append(part)
    return tuple(parts)


def _toml_table_declaration(line: str) -> Optional[tuple[tuple[str, ...], bool]]:
    header = _section_header(line)
    is_array = header.startswith("[[") and header.endswith("]]")
    if is_array:
        inner = header[2:-2]
    elif header.startswith("[") and header.endswith("]"):
        inner = header[1:-1]
    else:
        return None
    path = _toml_key_path(inner)
    return (path, is_array) if path is not None else None


def _toml_table_path(line: str) -> Optional[tuple[str, ...]]:
    declaration = _toml_table_declaration(line)
    return declaration[0] if declaration is not None else None


def _toml_assignment_path(line: str) -> Optional[tuple[str, ...]]:
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char == "#":
            return None
        if char in {'"', "'"}:
            quote = char
        elif char == "=":
            return _toml_key_path(line[:index])
    return None


def _toml_multiline_state_after(line: str, active: Optional[str]) -> Optional[str]:
    """Track TOML multiline strings without interpreting their contents as syntax."""
    index = 0
    quote: Optional[str] = None
    escaped = False
    while index < len(line):
        if active is not None:
            if line.startswith(active, index):
                preceding_backslashes = 0
                cursor = index - 1
                while cursor >= 0 and line[cursor] == "\\":
                    preceding_backslashes += 1
                    cursor -= 1
                if active == "'''" or preceding_backslashes % 2 == 0:
                    active = None
                    index += 3
                    continue
            index += 1
            continue
        char = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote is not None:
            if quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "#":
            break
        if line.startswith(('"""', "'''"), index):
            active = line[index:index + 3]
            index += 3
            continue
        if char in {'"', "'"}:
            quote = char
        index += 1
    return active


def _toml_line_metadata(text: str) -> tuple[list[Optional[tuple[str, ...]]], set[int]]:
    """Return table paths and lines whose first character is inside a multiline string."""
    table_paths: list[Optional[tuple[str, ...]]] = []
    string_lines: set[int] = set()
    active: Optional[str] = None
    for index, line in enumerate(text.splitlines()):
        if active is not None:
            string_lines.add(index)
            table_paths.append(None)
        else:
            table_paths.append(_toml_table_path(line))
        active = _toml_multiline_state_after(line, active)
    return table_paths, string_lines


def _mcp_section_starts(text: str) -> list[int]:
    table_paths, _ = _toml_line_metadata(text)
    lines = text.splitlines()
    return [
        index
        for index, path in enumerate(table_paths)
        if path == ("mcp_servers", "codex-lcm")
        and _toml_table_declaration(lines[index]) == (path, False)
    ]


def _has_conflicting_mcp_declaration(text: str) -> bool:
    """Detect declarations that cannot safely coexist with our managed table."""
    target = ("mcp_servers", "codex-lcm")
    _, string_lines = _toml_line_metadata(text)
    current_table: tuple[str, ...] = ()
    for index, line in enumerate(text.splitlines()):
        if index in string_lines:
            continue
        declaration = _toml_table_declaration(line)
        if declaration is not None:
            current_table, is_array = declaration
            if is_array and current_table[: len(target)] == target:
                return True
            continue
        key_path = _toml_assignment_path(line)
        if key_path is None:
            continue
        full_path = current_table + key_path
        inside_target = current_table[: len(target)] == target
        if not inside_target and (
            full_path == ("mcp_servers",)
            or full_path[: len(target)] == target
        ):
            return True
    return False


def _mcp_section_bounds(text: str) -> Optional[tuple[int, int]]:
    lines = text.splitlines()
    table_paths, _ = _toml_line_metadata(text)
    starts = _mcp_section_starts(text)
    if not starts:
        return None
    start = starts[0]
    end = start + 1
    while end < len(lines) and table_paths[end] is None:
        end += 1
    return start, end


def _toml_array_end(lines: list[str], start: int, section_end: int, first_value: str) -> int:
    depth = 0
    started = False
    for line_index in range(start, section_end):
        line = first_value if line_index == start else lines[line_index]
        quote: Optional[str] = None
        escaped = False
        for char in line:
            if escaped:
                escaped = False
                continue
            if quote is not None:
                if quote == '"' and char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char == "#":
                break
            if char in {'"', "'"}:
                quote = char
            elif char == "[":
                depth += 1
                started = True
            elif char == "]":
                depth -= 1
                if started and depth == 0:
                    return line_index + 1
        if quote is not None:
            raise ValueError("codex-lcm MCP args contain an unsupported multiline string")
    raise ValueError("codex-lcm MCP args contain an unterminated array")


def _mcp_section_data(text: str) -> tuple[dict[str, Any], dict[str, int], set[int]]:
    bounds = _mcp_section_bounds(text)
    if bounds is None:
        return {}, {}, set()
    start, end = bounds
    lines = text.splitlines()
    _, string_lines = _toml_line_metadata(text)
    values: dict[str, Any] = {}
    counts: dict[str, int] = {}
    managed_lines: set[int] = set()
    index = start + 1
    while index < end:
        if index in string_lines:
            index += 1
            continue
        line = lines[index]
        if "=" not in line:
            index += 1
            continue
        raw_key, value = line.split("=", 1)
        key = _toml_key_name(raw_key)
        if key in {"command", "args"}:
            span_end = index + 1
            if key == "args" and value.lstrip().startswith("["):
                span_end = _toml_array_end(lines, index, end, value)
                value = "\n".join([value, *lines[index + 1:span_end]])
            elif key == "command" and value.lstrip().startswith(('"""', "'''")):
                raise ValueError("codex-lcm MCP command must use a single-line TOML string")
            values[key] = _toml_literal(value)
            counts[key] = counts.get(key, 0) + 1
            managed_lines.update(range(index, span_end))
            index = span_end
            continue
        index += 1
    return values, counts, managed_lines


def _mcp_server_is_wired(text: str) -> bool:
    """Return whether the codex-lcm MCP section points to this installation."""
    try:
        _validate_toml_config(text)
    except ValueError:
        return False
    if len(_mcp_section_starts(text)) != 1:
        return False
    try:
        values, counts, _ = _mcp_section_data(text)
    except ValueError:
        return False
    return (
        counts.get("command") == 1
        and counts.get("args") == 1
        and values.get("command") == sys.executable
        and values.get("args") == ["-m", f"{_PKG}.mcp_server"]
    )


def _render_mcp_server(text: str) -> tuple[str, bool]:
    _validate_toml_config(text)
    starts = _mcp_section_starts(text)
    if len(starts) > 1:
        raise ValueError("config.toml contains multiple codex-lcm MCP sections")
    if _has_conflicting_mcp_declaration(text):
        raise ValueError("config.toml declares codex-lcm outside its managed section")
    if not starts:
        updated = text.rstrip("\n") + "\n" + _mcp_block()
        _validate_toml_config(updated)
        return updated, True
    if _mcp_server_is_wired(text):
        return text, False

    bounds = _mcp_section_bounds(text)
    if bounds is None:  # pragma: no cover - guarded by starts above
        raise ValueError("could not locate the codex-lcm MCP section")
    start, end = bounds
    _, _, managed_lines = _mcp_section_data(text)
    lines = text.splitlines()
    retained = [lines[index] for index in range(start + 1, end) if index not in managed_lines]
    replacement = [
        _MCP_SECTION,
        f"command = {json.dumps(sys.executable)}",
        f"args = [\"-m\", \"{_PKG}.mcp_server\"]",
        *retained,
    ]
    lines[start:end] = replacement
    updated = "\n".join(lines).rstrip("\n") + "\n"
    _validate_toml_config(updated)
    return updated, True


def _ensure_mcp_server(config_path: Path) -> bool:
    """Add or repair the codex-lcm MCP block. Returns True if changed."""
    text = config_path.read_text() if config_path.exists() else ""
    updated, changed = _render_mcp_server(text)
    if not changed:
        return False
    _write_text(config_path, updated)
    return True


def _render_without_mcp_server(text: str) -> tuple[str, bool]:
    """Render config without owned MCP sections, failing closed on invalid TOML."""
    _validate_toml_config(text)
    lines = text.splitlines()
    table_paths, _ = _toml_line_metadata(text)
    remove: set[int] = set()
    base_starts: list[int] = []
    for start, path in enumerate(table_paths):
        if path is None or path[:2] != ("mcp_servers", "codex-lcm"):
            continue
        if path == ("mcp_servers", "codex-lcm"):
            base_starts.append(start)
        end = start + 1
        while end < len(lines) and table_paths[end] is None:
            end += 1
        remove.update(range(start, end))
    for start in base_starts:
        if start > 0 and lines[start - 1].strip() == _MCP_MARKER:
            remove.add(start - 1)
            if start > 1 and not lines[start - 2].strip():
                remove.add(start - 2)
    if not remove:
        return text, False
    out = [line for index, line in enumerate(lines) if index not in remove]
    # Trim trailing blank lines left by removal.
    while out and out[-1].strip() == "":
        out.pop()
    return "\n".join(out) + "\n", True


def _remove_mcp_server(config_path: Path) -> bool:
    """Remove codex-lcm MCP sections without consuming neighbouring settings."""
    if not config_path.exists():
        return False
    updated, changed = _render_without_mcp_server(config_path.read_text())
    if not changed:
        return False
    _write_text(config_path, updated)
    return True


# --- commands --------------------------------------------------------------


def cmd_install(_args: argparse.Namespace) -> int:
    config = LCMConfig()
    db = LCMDatabase(config)
    db.conn  # initialize schema
    db.close()

    home = Path.home()
    codex_dir = home / ".codex"
    if not codex_dir.exists():
        print(f"ERROR: Codex config directory not found at {codex_dir}. Install Codex CLI first.")
        return 1

    python = _python_cmd()
    changes: list[str] = []

    # Preflight both configuration files before writing either one.
    hooks_path = codex_dir / "hooks.json"
    config_path = codex_dir / "config.toml"
    try:
        existing_hooks = _load_json(hooks_path)
        merged, added_hooks = _merge_hooks(existing_hooks, _build_hooks(python))
        config_text = config_path.read_text() if config_path.exists() else ""
        rendered_config, update_mcp = _render_mcp_server(config_text)
    except (OSError, ValueError) as exc:
        print(f"ERROR: Refusing to replace incompatible Codex configuration: {exc}")
        return 1

    # 1. hooks.json
    if added_hooks:
        _backup(hooks_path)
        _write_text(hooks_path, json.dumps(merged, indent=2) + "\n")
        changes.append(f"hooks → {hooks_path}")

    # 2. config.toml MCP server
    if update_mcp:
        _backup(config_path)
        _write_text(config_path, rendered_config)
        changes.append(f"MCP server → {config_path}")

    print("codex-lcm installed.")
    print(f"  data dir : {config.db_path.parent}")
    print(f"  db       : {config.db_path}")
    for c in changes:
        print(f"  wired    : {c}")
    if not changes:
        print("  (already installed — no changes made)")
    print("\nNext: run `codex-lcm doctor` to verify, then restart Codex.")
    return 0


def cmd_uninstall(_args: argparse.Namespace) -> int:
    home = Path.home()
    codex_dir = home / ".codex"

    hooks_path = codex_dir / "hooks.json"
    config_path = codex_dir / "config.toml"
    try:
        existing_hooks = _load_json(hooks_path) if hooks_path.exists() else {}
        cleaned_hooks = _strip_codex_lcm_hooks(existing_hooks)
        config_text = config_path.read_text() if config_path.exists() else ""
        rendered_config, remove_mcp = _render_without_mcp_server(config_text)
    except (OSError, ValueError) as exc:
        print(f"ERROR: Refusing to modify incompatible Codex configuration: {exc}")
        return 1

    if cleaned_hooks != existing_hooks:
        _backup(hooks_path)
        _write_text(hooks_path, json.dumps(cleaned_hooks, indent=2) + "\n")
        print(f"Removed codex-lcm hooks from {hooks_path}")

    if config_path.exists() and remove_mcp:
        _backup(config_path)
        _write_text(config_path, rendered_config)
        print(f"Removed codex-lcm MCP server from {config_path}")

    print("Uninstall complete. Your data remains at ~/.codex-lcm/ (delete it manually if desired).")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    config = LCMConfig()
    db = LCMDatabase(config)
    s = db.stats()
    db.close()
    print(f"codex-lcm v{__version__}")
    print(f"  db        : {s['db_path']}")
    print(f"  size      : {s['db_size_mb']} MB")
    print(f"  sessions  : {s['sessions']}")
    print(f"  messages  : {s['messages']}")
    print(f"  summaries : {s['summaries']}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    home = Path.home()
    codex_dir = home / ".codex"
    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "✓" if passed else "✗"
        print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
        if not passed:
            ok = False

    print("codex-lcm doctor:")

    # Package importable
    try:
        import codex_lcm  # noqa: F401
        check("package importable", True, codex_lcm.__version__)
    except Exception as e:
        check("package importable", False, str(e))

    # DB healthy
    try:
        config = LCMConfig()
        db = LCMDatabase(config)
        tables = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        db.close()
        check("database healthy", True, f"{len(tables)} tables")
    except Exception as e:
        check("database healthy", False, str(e))

    # hooks.json wired
    hooks_path = codex_dir / "hooks.json"
    if hooks_path.exists():
        try:
            existing_hooks = _load_json(hooks_path)
            _, would_change = _merge_hooks(existing_hooks, _build_hooks(_python_cmd()))
            wired = not would_change
        except ValueError:
            wired = False
        check("hooks wired", wired, str(hooks_path))
    else:
        check("hooks wired", False, f"{hooks_path} missing (run install)")

    # config.toml MCP wired
    config_path = codex_dir / "config.toml"
    if config_path.exists():
        wired = _mcp_server_is_wired(config_path.read_text())
        check("MCP server wired", wired, str(config_path))
    else:
        check("MCP server wired", False, f"{config_path} missing (run install)")

    # Hook python can import the package
    check("hook python resolves package", True, sys.executable)

    print("\n" + ("All checks passed." if ok else "Some checks failed — run `codex-lcm install`."))
    return 0 if ok else 1


def cmd_compact(args: argparse.Namespace) -> int:
    if args.all:
        config = LCMConfig()
        db = LCMDatabase(config)
        sessions = [s["session_id"] for s in db.list_sessions()]
        db.close()
        if not sessions:
            print("No sessions to compact.")
            return 0
        for sid in sessions:
            print(f"Compacting {sid} ...")
            run_worker(sid, synchronous=True)
        return 0

    if not args.session:
        print("Provide --session <id> or --all.")
        return 1
    run_worker(args.session, synchronous=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-lcm", description="Lossless Context Management for Codex CLI")
    parser.add_argument("--version", action="version", version=f"codex-lcm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install", help="Wire codex-lcm hooks + MCP server into Codex")
    sub.add_parser("uninstall", help="Remove codex-lcm hooks + MCP server (keeps data)")
    sub.add_parser("status", help="Show database status")
    sub.add_parser("doctor", help="Verify the installation is healthy")

    p_compact = sub.add_parser("compact", help="Run compaction on a session")
    p_compact.add_argument("--session", help="Session id to compact")
    p_compact.add_argument("--all", action="store_true", help="Compact all sessions")

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "compact": cmd_compact,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        sys.exit(handler(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
