"""Tests for the CLI: hooks merge, MCP config wiring, backups, uninstall."""

import json
import os
import argparse

import pytest

from codex_lcm import cli


def test_build_hooks_has_all_events():
    hooks = cli._build_hooks("python3")
    assert set(hooks.keys()) == {
        "SessionStart", "UserPromptSubmit", "PostToolUse",
        "PreCompact", "PostCompact", "Stop",
    }


def test_merge_hooks_idempotent_and_preserves_existing():
    existing = {
        "description": "mine",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "node my_hook.js"}]}],
        },
    }
    merged, added = cli._merge_hooks(existing, cli._build_hooks("python3"))
    assert added is True
    # Existing hook preserved.
    assert any("my_hook.js" in json.dumps(g) for g in merged["hooks"]["Stop"])

    # Second merge: nothing added (idempotent).
    merged2, added2 = cli._merge_hooks(merged, cli._build_hooks("python3"))
    assert added2 is False


def test_merge_hooks_does_not_mutate_input():
    existing = {"hooks": {}}
    cli._merge_hooks(existing, cli._build_hooks("python3"))
    assert existing == {"hooks": {}}, "caller's dict must not be mutated"


def test_merge_hooks_repairs_wrong_codex_lcm_event_command():
    wrong_command = "/dead/python -m codex_lcm.hooks.user_prompt_submit"
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": wrong_command, "timeout": 15},
                    ]
                }
            ]
        }
    }

    merged, changed = cli._merge_hooks(existing, cli._build_hooks("python3"))

    assert changed is True
    assert wrong_command not in json.dumps(merged["hooks"]["Stop"])
    assert "python3 -m codex_lcm.hooks.stop" in json.dumps(merged["hooks"]["Stop"])


def test_merge_hooks_removes_stale_codex_lcm_command_from_unmanaged_event():
    existing = {"hooks": cli._build_hooks("python3")}
    existing["hooks"]["PreToolUse"] = [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 -m codex_lcm.hooks.stop",
                }
            ]
        }
    ]

    merged, changed = cli._merge_hooks(existing, cli._build_hooks("python3"))

    assert changed is True
    assert "PreToolUse" not in merged["hooks"]


@pytest.mark.parametrize(
    "existing",
    [
        {"hooks": "keep-this-value"},
        {"hooks": {"Stop": "keep-this-event-value"}},
    ],
)
def test_merge_hooks_rejects_incompatible_existing_shapes(existing):
    with pytest.raises(ValueError, match="hooks"):
        cli._merge_hooks(existing, cli._build_hooks("python3"))


def test_strip_only_codex_lcm_hooks():
    hooks = cli._build_hooks("python3")
    merged, _ = cli._merge_hooks({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "node keep.js"}]}]}}, hooks)
    cleaned = cli._strip_codex_lcm_hooks(merged)
    assert "codex_lcm" not in json.dumps(cleaned)
    assert any("keep.js" in json.dumps(g) for g in cleaned["hooks"]["Stop"])


def test_strip_preserves_third_party_hook_in_mixed_group():
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 -m codex_lcm.hooks.stop",
                            "timeout": 15,
                        },
                        {"type": "command", "command": "node keep.js"},
                    ]
                }
            ]
        }
    }

    cleaned = cli._strip_codex_lcm_hooks(existing)

    assert "codex_lcm" not in json.dumps(cleaned)
    assert "node keep.js" in json.dumps(cleaned)


def test_strip_preserves_unrelated_empty_hook_event():
    existing = {
        "hooks": {
            "CustomEvent": [],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 -m codex_lcm.hooks.stop",
                            "timeout": 15,
                        }
                    ]
                }
            ],
        }
    }

    cleaned = cli._strip_codex_lcm_hooks(existing)

    assert cleaned == {"hooks": {"CustomEvent": []}}


def test_hook_detection_does_not_match_an_unrelated_command_argument():
    group = {
        "hooks": [
            {
                "type": "command",
                "command": "python custom_hook.py --note codex_lcm",
            }
        ]
    }

    assert cli._group_is_codex_lcm(group) is False


def test_mcp_server_round_trip(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[mcp_servers.github]\ncommand = \"gh\"\n")

    assert cli._ensure_mcp_server(cfg) is True
    assert "codex-lcm" in cfg.read_text()
    # Idempotent.
    assert cli._ensure_mcp_server(cfg) is False

    assert cli._remove_mcp_server(cfg) is True
    assert "codex-lcm" not in cfg.read_text()
    # GitHub block survives.
    assert "mcp_servers.github" in cfg.read_text()
    assert cli._remove_mcp_server(cfg) is False


def test_remove_mcp_server_preserves_quoted_neighbor_with_hash(tmp_path):
    cfg = tmp_path / "config.toml"
    neighbor = '["custom#table"]\nvalue = "preserve me"\n'
    cfg.write_text(cli._mcp_block() + neighbor)

    assert cli._remove_mcp_server(cfg) is True
    assert cfg.read_text() == neighbor


def test_mcp_comment_does_not_count_as_server_wiring(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("# codex-lcm is mentioned here only\n[mcp_servers.github]\ncommand = \"gh\"\n")

    assert cli._mcp_server_is_wired(cfg.read_text()) is False
    assert cli._ensure_mcp_server(cfg) is True
    assert cli._mcp_server_is_wired(cfg.read_text()) is True
    assert cfg.read_text().count("[mcp_servers.codex-lcm]") == 1


def test_mcp_section_with_wrong_command_is_not_wired(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.codex-lcm]\n"
        'command = "/wrong/python"\n'
        'args = ["-m", "not_codex_lcm"]\n'
    )

    assert cli._mcp_server_is_wired(cfg.read_text()) is False


def test_ensure_mcp_server_repairs_command_and_preserves_other_settings(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.codex-lcm]\n"
        'command = "/wrong/python"\n'
        'args = ["-m", "not_codex_lcm"]\n'
        "enabled = true\n"
    )

    assert cli._ensure_mcp_server(cfg) is True
    assert cli._mcp_server_is_wired(cfg.read_text()) is True
    assert "enabled = true" in cfg.read_text()


def test_ensure_mcp_server_repairs_multiline_args_without_corrupting_toml(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp_servers."codex-lcm"]\n'
        '"command" = "/wrong/python"\n'
        '"args" = [ # valid TOML comment\n'
        '  "-m",\n'
        '  "codex_lcm.mcp_server",\n'
        "]\n"
        "enabled = true\n"
    )

    assert cli._ensure_mcp_server(cfg) is True
    updated = cfg.read_text()
    assert cli._mcp_server_is_wired(updated) is True
    assert updated.count("codex_lcm.mcp_server") == 1
    assert "enabled = true" in updated


def test_ensure_mcp_server_ignores_table_like_text_in_multiline_string(tmp_path):
    tomllib = pytest.importorskip("tomllib")
    cfg = tmp_path / "config.toml"
    original = (
        'note = """\n'
        "[mcp_servers.codex-lcm]\n"
        'command = "example-only"\n'
        'args = ["-m", "example.module"]\n'
        "[mcp_servers.github]\n"
        'command = "also-example-only"\n'
        '"""\n'
    )
    cfg.write_text(original)
    original_note = tomllib.loads(original)["note"]

    assert cli._ensure_mcp_server(cfg) is True

    parsed = tomllib.loads(cfg.read_text())
    assert parsed["note"] == original_note
    assert parsed["mcp_servers"]["codex-lcm"] == {
        "command": cli.sys.executable,
        "args": ["-m", "codex_lcm.mcp_server"],
    }


def test_ensure_mcp_server_preserves_symlinked_config(tmp_path):
    target = tmp_path / "managed-config.toml"
    target.write_text('[mcp_servers.github]\ncommand = "gh"\n')
    cfg = tmp_path / "config.toml"
    cfg.symlink_to(target)

    assert cli._ensure_mcp_server(cfg) is True

    assert cfg.is_symlink()
    assert cli._mcp_server_is_wired(target.read_text()) is True


def test_equivalent_quoted_mcp_section_is_detected_without_rewrite(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp_servers."codex-lcm"]\n'
        f'"command" = {json.dumps(cli.sys.executable)}\n'
        '"args" = [\n'
        '  "-m",\n'
        '  "codex_lcm.mcp_server",\n'
        "]\n"
        "enabled = true\n"
    )
    before = cfg.read_bytes()

    assert cli._mcp_server_is_wired(cfg.read_text()) is True
    assert cli._ensure_mcp_server(cfg) is False
    assert cfg.read_bytes() == before


def test_duplicate_mcp_sections_are_rejected_without_modification(tmp_path):
    cfg = tmp_path / "config.toml"
    quoted_block = cli._mcp_block().replace(
        "[mcp_servers.codex-lcm]", '[mcp_servers."codex-lcm"]'
    )
    cfg.write_text(cli._mcp_block() + quoted_block)
    before = cfg.read_bytes()

    assert cli._mcp_server_is_wired(cfg.read_text()) is False
    with pytest.raises(ValueError, match="config.toml"):
        cli._ensure_mcp_server(cfg)
    assert cfg.read_bytes() == before


@pytest.mark.parametrize(
    "declaration",
    [
        (
            'mcp_servers.codex-lcm = { command = "/old/python", '
            'args = ["-m", "codex_lcm.mcp_server"] }\n'
        ),
        'mcp_servers."codex-lcm".command = "/old/python"\n',
        '[mcp_servers]\ncodex-lcm = { command = "/old/python" }\n',
        'mcp_servers = { codex-lcm = { command = "/old/python" } }\n',
        '[[mcp_servers.codex-lcm]]\ncommand = "/old/python"\n',
    ],
)
def test_conflicting_mcp_declaration_is_rejected_without_modification(tmp_path, declaration):
    cfg = tmp_path / "config.toml"
    cfg.write_text(declaration)
    before = cfg.read_bytes()

    with pytest.raises(ValueError, match="config.toml"):
        cli._ensure_mcp_server(cfg)

    assert cfg.read_bytes() == before


def test_remove_mcp_server_preserves_indented_following_section(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.codex-lcm]\n"
        'command = "python3"\n'
        'args = ["-m", "codex_lcm.mcp_server"]\n'
        "\n"
        "  [mcp_servers.github]\n"
        '  command = "gh"\n'
    )

    assert cli._remove_mcp_server(cfg) is True
    assert "mcp_servers.github" in cfg.read_text()
    assert 'command = "gh"' in cfg.read_text()


def test_remove_mcp_server_preserves_following_array_table(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.codex-lcm]\n"
        'command = "python3"\n'
        'args = ["-m", "codex_lcm.mcp_server"]\n'
        "\n"
        "[[profiles]]\n"
        'name = "keep-me"\n'
    )

    assert cli._remove_mcp_server(cfg) is True

    assert cfg.read_text() == '[[profiles]]\nname = "keep-me"\n'


def test_remove_mcp_server_preserves_unrelated_setting_after_stray_marker(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "# codex-lcm — lossless context management MCP server\n"
        "unrelated_setting = true\n"
        "[mcp_servers.codex-lcm]\n"
        'command = "python3"\n'
        'args = ["-m", "codex_lcm.mcp_server"]\n'
    )

    assert cli._remove_mcp_server(cfg) is True
    assert "unrelated_setting = true" in cfg.read_text()


@pytest.mark.parametrize(
    "malformed",
    [
        "answer = [1,,2]\n",
        (
            "[mcp_servers.codex-lcm]\n"
            'command = "/venv/bin/python"\n'
            'args = ["-m", "codex_lcm.mcp_server"]\n'
            'note = """\n'
            "[mcp_servers.github]\n"
            'command = "gh"\n'
        ),
    ],
)
def test_mcp_mutation_rejects_malformed_toml_without_modification(tmp_path, malformed):
    cfg = tmp_path / "config.toml"
    cfg.write_text(malformed)
    before = cfg.read_bytes()

    with pytest.raises(ValueError, match="valid TOML"):
        cli._ensure_mcp_server(cfg)
    assert cfg.read_bytes() == before

    with pytest.raises(ValueError, match="valid TOML"):
        cli._remove_mcp_server(cfg)
    assert cfg.read_bytes() == before


def test_load_json_rejects_malformed_configuration(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("{not-json")

    with pytest.raises(ValueError, match="valid JSON"):
        cli._load_json(path)


def test_backup_creates_timestamped_file(tmp_path):
    src = tmp_path / "hooks.json"
    src.write_text("{}")
    backup = cli._backup(src)
    assert backup is not None and backup.exists()
    assert backup.read_text() == "{}"
    if os.name == "posix":
        src.chmod(0o600)
        private_backup = cli._backup(src)
        assert private_backup is not None
        assert private_backup.stat().st_mode & 0o777 == 0o600
    # No backup when the file doesn't exist.
    assert cli._backup(tmp_path / "missing") is None


def test_uninstall_is_byte_for_byte_idempotent_when_not_wired(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"node keep.js"}]}]}}\n'
    )
    config.write_text('[mcp_servers.github]\ncommand = "gh"\n')
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()
    monkeypatch.setenv("HOME", str(home))

    assert cli.cmd_uninstall(argparse.Namespace()) == 0
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before


def test_doctor_rejects_mcp_section_with_wrong_command(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(json.dumps({"hooks": cli._build_hooks(cli._python_cmd())}))
    (codex_dir / "config.toml").write_text(
        "[mcp_servers.codex-lcm]\n"
        'command = "/wrong/python"\n'
        'args = ["-m", "not_codex_lcm"]\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "doctor.db"))

    assert cli.cmd_doctor(argparse.Namespace()) == 1


def test_doctor_rejects_hook_wired_to_wrong_event_module(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = cli._build_hooks(cli._python_cmd())
    hooks["Stop"][0]["hooks"][0]["command"] = (
        f"{cli._python_cmd()} -m codex_lcm.hooks.user_prompt_submit"
    )
    (codex_dir / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    (codex_dir / "config.toml").write_text(cli._mcp_block())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "doctor.db"))

    assert cli.cmd_doctor(argparse.Namespace()) == 1


def test_doctor_rejects_malformed_toml_even_when_owned_fields_look_wired(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_text(
        json.dumps({"hooks": cli._build_hooks(cli._python_cmd())})
    )
    (codex_dir / "config.toml").write_text(
        cli._mcp_block()
        + 'note = """\n'
        + "[mcp_servers.github]\n"
        + 'command = "gh"\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "doctor.db"))

    assert cli.cmd_doctor(argparse.Namespace()) == 1


def test_install_rejects_incompatible_hooks_without_modifying_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_text('{"hooks":"keep-this-value"}\n')
    config.write_text('[mcp_servers.github]\ncommand = "gh"\n')
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "install.db"))

    assert cli.cmd_install(argparse.Namespace()) == 1
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before


def test_install_preflights_duplicate_mcp_sections_before_writing_hooks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"node keep.js"}]}]}}\n'
    )
    config.write_text(cli._mcp_block() + cli._mcp_block())
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "install.db"))

    assert cli.cmd_install(argparse.Namespace()) == 1
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before


def test_install_rejects_malformed_toml_before_writing_either_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"node keep.js"}]}]}}\n'
    )
    config.write_text("answer = [1,,2]\n")
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "install.db"))

    assert cli.cmd_install(argparse.Namespace()) == 1
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before


@pytest.mark.parametrize(
    "config_text",
    [
        (
            "[mcp_servers.codex-lcm]\n"
            'command.path = "/old/python"\n'
            'args = ["-m", "legacy.module"]\n'
            "\n"
            "[mcp_servers.github]\n"
            'command = "gh"\n'
        ),
        (
            "[mcp_servers.codex-lcm]\n"
            'args = ["-m", "legacy.module"]\n'
            "\n"
            "[mcp_servers.codex-lcm.command]\n"
            'path = "/old/python"\n'
            "\n"
            "[mcp_servers.github]\n"
            'command = "gh"\n'
        ),
    ],
)
def test_install_rejects_owned_child_tables_before_writing_either_config(
    tmp_path, monkeypatch, config_text
):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"node keep.js"}]}]}}\n'
    )
    config.write_text(config_text)
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_LCM_DB_PATH", str(tmp_path / "install.db"))

    assert cli.cmd_install(argparse.Namespace()) == 1
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before


def test_uninstall_rejects_unterminated_toml_before_writing_either_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_text(json.dumps({"hooks": cli._build_hooks(cli._python_cmd())}) + "\n")
    config.write_text(
        "[mcp_servers.codex-lcm]\n"
        'command = "/venv/bin/python"\n'
        'args = ["-m", "codex_lcm.mcp_server"]\n'
        'note = """\n'
        "[mcp_servers.github]\n"
        'command = "gh"\n'
    )
    hooks_before = hooks.read_bytes()
    config_before = config.read_bytes()
    monkeypatch.setenv("HOME", str(home))

    assert cli.cmd_uninstall(argparse.Namespace()) == 1
    assert hooks.read_bytes() == hooks_before
    assert config.read_bytes() == config_before
