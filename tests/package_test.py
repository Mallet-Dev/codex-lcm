"""Build/install/doctor/uninstall smoke test in a throwaway HOME and venv."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


FAILURES: list[str] = []
ROOT = Path(__file__).resolve().parents[1]


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-lcm-package-") as tmp:
        temp = Path(tmp)
        home = temp / "home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        hooks_path = codex_dir / "hooks.json"
        config_path = codex_dir / "config.toml"
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "python keep_existing.py"}]}
                        ]
                    }
                }
            )
        )
        config_path.write_text(
            '# codex-lcm is mentioned in an unrelated comment\n'
            '[mcp_servers.github]\ncommand = "gh"\n'
        )

        source = temp / "source"
        shutil.copytree(
            ROOT,
            source,
            ignore=shutil.ignore_patterns(".git", "*.egg-info", "__pycache__", "build", "dist", ".venv"),
        )

        venv = temp / "venv"
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        check("throwaway virtual environment created", created.returncode == 0, created.stderr.strip())
        if created.returncode != 0:
            return 1

        python = venv / "bin" / "python"
        pip = venv / "bin" / "pip"
        command = venv / "bin" / "codex-lcm"
        env = {
            **os.environ,
            "HOME": str(home),
            "CODEX_LCM_DB_PATH": str(home / ".codex-lcm" / "codex-lcm.db"),
        }

        installed = run(
            [str(pip), "install", "--no-deps", str(source)],
            cwd=temp,
            env=env,
        )
        check("wheel build and isolated install succeed", installed.returncode == 0, installed.stderr.strip())
        if installed.returncode != 0:
            return 1

        imported = run(
            [str(python), "-c", "import codex_lcm; print(codex_lcm.__version__)"],
            cwd=temp,
            env=env,
        )
        check("installed package imports outside source tree", imported.returncode == 0, imported.stderr.strip())
        if imported.returncode != 0:
            return 1

        manifest = run(
            [
                str(python),
                "-c",
                (
                    "import json; from importlib.metadata import distribution; "
                    "d=distribution('codex-lcm'); "
                    "print(json.dumps({'version': d.version, 'files': [str(p) for p in (d.files or [])]}))"
                ),
            ],
            cwd=temp,
            env=env,
        )
        check("installed distribution metadata is readable", manifest.returncode == 0, manifest.stderr.strip())
        if manifest.returncode == 0:
            metadata = json.loads(manifest.stdout)
            files = metadata["files"]
            check(
                "distribution version matches package version",
                metadata["version"] == imported.stdout.strip(),
                metadata["version"],
            )
            unexpected = [
                path
                for path in files
                if path.startswith(("tests/", "docs/"))
            ]
            check(
                "distribution contains runtime package only",
                not unexpected,
                str(unexpected),
            )
            check(
                "distribution includes the project licence",
                any(path.endswith("licenses/LICENSE") for path in files),
                str(files),
            )

        install = run([str(command), "install"], cwd=temp, env=env)
        check("installed CLI wires hooks and MCP", install.returncode == 0, install.stdout.strip())

        doctor = run([str(command), "doctor"], cwd=temp, env=env)
        check("doctor passes in isolated installed environment", doctor.returncode == 0, doctor.stdout.strip())

        wired_hooks = hooks_path.read_text()
        wired_config = config_path.read_text()
        check("install preserves unrelated hook", "keep_existing.py" in wired_hooks)
        check("install preserves unrelated MCP server", "mcp_servers.github" in wired_config)
        check("install creates exact codex-lcm MCP section", "[mcp_servers.codex-lcm]" in wired_config)
        check("install creates isolated database", (home / ".codex-lcm" / "codex-lcm.db").exists())
        if os.name == "posix":
            data_dir = home / ".codex-lcm"
            database = data_dir / "codex-lcm.db"
            check("data directory is private", data_dir.stat().st_mode & 0o777 == 0o700)
            check("database is private", database.stat().st_mode & 0o777 == 0o600)

        uninstall = run([str(command), "uninstall"], cwd=temp, env=env)
        check("installed CLI uninstalls cleanly", uninstall.returncode == 0, uninstall.stdout.strip())
        check("uninstall preserves unrelated hook", "keep_existing.py" in hooks_path.read_text())
        check("uninstall preserves unrelated MCP server", "mcp_servers.github" in config_path.read_text())
        check("uninstall preserves unrelated codex-lcm comment", "mentioned in an unrelated comment" in config_path.read_text())
        check("uninstall removes only codex-lcm hook wiring", "codex_lcm" not in hooks_path.read_text())
        check("uninstall removes only codex-lcm MCP wiring", "[mcp_servers.codex-lcm]" not in config_path.read_text())

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"PACKAGE GAUNTLET FAILED: {len(FAILURES)} checks")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("PACKAGE GAUNTLET PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
