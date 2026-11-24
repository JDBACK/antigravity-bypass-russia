"""Offline regression tests for scripts/ag_patch.sh using mock engine and mock ssh."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ag_patch.sh"


def write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_mock_engine(directory: Path, inspect_payload: dict, patch_rc: int = 0) -> tuple[Path, Path]:
    log_file = directory / "engine.log"
    payload_file = directory / "inspect.json"
    payload_file.write_text(json.dumps(inspect_payload), encoding="utf-8")
    engine = write_executable(
        directory / "mock-engine",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'CALL:%s|PROMPT=%s\\n' "$*" "${{AGY_PROMPT_CLOSE:-}}" >> {str(log_file)!r}
case "${{1:-}}" in
  inspect-files)
    cat {str(payload_file)!r}
    ;;
  status)
    echo "MOCK STATUS OK"
    ;;
  patch-files|restore-files)
    echo "MOCK $1 DONE"
    exit {patch_rc}
    ;;
  *)
    echo "UNEXPECTED $*" >&2
    exit 99
    ;;
esac
""",
    )
    return engine, log_file


def make_mock_ssh(directory: Path, reachable: bool = True, remote_rc: int = 0) -> tuple[Path, Path]:
    log_file = directory / "ssh.log"
    probe_rc = 0 if reachable else 255
    ssh = write_executable(
        directory / "mock-ssh",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'SSH:%s\\n' "$*" >> {str(log_file)!r}
for arg in "$@"; do
  if [[ $arg == "true" ]]; then
    exit {probe_rc}
  fi
done
echo "=== mock-remote (Linux) ==="
echo "MOCK REMOTE RUN OK"
exit {remote_rc}
""",
    )
    return ssh, log_file


def run_script(args, env_overrides=None, stdin_text=None):
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def sample_payload(needs_patch=1, needs_restore=1, holders="", writable=True):
    return {
        "targets": [
            {
                "kind": "antigravity",
                "label": "Antigravity",
                "name": "language_server",
                "path": "/Applications/Antigravity.app/Contents/Resources/bin/language_server",
                "state": "stock" if needs_patch else "patched",
                "needs_patch": bool(needs_patch),
                "needs_restore": bool(needs_restore),
                "writable": writable,
            }
        ],
        "needs_patch_count": needs_patch,
        "needs_restore_count": needs_restore,
        "needs_admin_for_patch": not writable and bool(needs_patch),
        "needs_admin_for_restore": not writable and bool(needs_restore),
        "patch_holders": [f"{holders} (PID 123)"] if holders else [],
        "patch_holders_summary": holders,
        "restore_holders": [f"{holders} (PID 123)"] if holders else [],
        "restore_holders_summary": holders,
        "holders_error": None,
    }


def main():
    if sys.platform == "win32":
        print("SKIP: ag_patch.sh is Unix-only")
        return

    with tempfile.TemporaryDirectory(prefix="ag-patch-test-") as tmp:
        root = Path(tmp)

        # 1. --help and invalid option
        help_res = run_script(["--help"])
        assert help_res.returncode == 0, help_res
        assert "ag-patch" in help_res.stdout and "--dry-run" in help_res.stdout

        bad_res = run_script(["--bogus"])
        assert bad_res.returncode == 2, bad_res
        assert "Неизвестный аргумент" in bad_res.stderr

        # 2. macOS auto: patches MacBook with --prompt-close, then SSHes to homelinux
        case = root / "mac-both"
        case.mkdir()
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=1, holders="Antigravity (2 процесса)"))
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        res = run_script(
            [],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
                "AG_PATCH_SSH_HOST": "homelinux",
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "MacBook (macOS)" in res.stdout
        assert "=== Итог ===" in res.stdout
        assert "✓ MacBook: готово" in res.stdout
        assert "✓ homelinux: готово" in res.stdout
        calls = engine_log.read_text(encoding="utf-8")
        assert "CALL:inspect-files --json|" in calls
        assert "CALL:patch-files --prompt-close|PROMPT=1" in calls
        ssh_calls = ssh_log.read_text(encoding="utf-8")
        assert "homelinux" in ssh_calls and "--local" in ssh_calls

        # 3. macOS auto when MacBook is already patched: skips patch-files on MacBook, still runs Linux
        case = root / "mac-clean"
        case.mkdir()
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=0))
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        res = run_script(
            [],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        calls = engine_log.read_text(encoding="utf-8")
        assert "patch-files" not in calls, calls
        assert "уже настроены" in res.stdout
        assert "homelinux" in ssh_log.read_text(encoding="utf-8")

        # 4. --dry-run never calls patch-files or restore-files, shows holders and forwards --dry-run over SSH
        case = root / "dry-run"
        case.mkdir()
        engine, engine_log = make_mock_engine(
            case, sample_payload(needs_patch=1, holders="Antigravity (3 процесса)", writable=True)
        )
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        res = run_script(
            ["--dry-run"],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        calls = engine_log.read_text(encoding="utf-8")
        assert "patch-files" not in calls and "restore-files" not in calls
        assert "[dry-run]" in res.stdout
        assert "Antigravity (3 процесса)" in res.stdout
        assert "без sudo" in res.stdout
        assert "--dry-run" in ssh_log.read_text(encoding="utf-8")

        # 5. --status runs status + inspect-files on MacBook and forwards --status to Linux
        case = root / "status"
        case.mkdir()
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=1))
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        res = run_script(
            ["--status"],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "MOCK STATUS OK" in res.stdout
        assert "--status" in ssh_log.read_text(encoding="utf-8")

        # 6. --restore calls restore-files --prompt-close and forwards --restore
        case = root / "restore"
        case.mkdir()
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=0, needs_restore=1))
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        res = run_script(
            ["--restore"],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "CALL:restore-files --prompt-close|PROMPT=1" in engine_log.read_text(encoding="utf-8")
        assert "--restore" in ssh_log.read_text(encoding="utf-8")

        # 7. --mac skips SSH completely; --linux skips local engine completely
        case = root / "scope"
        case.mkdir()
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=1))
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        res = run_script(
            ["--mac"],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert not ssh_log.exists()

        engine_log.unlink()
        res = run_script(
            ["--linux"],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert not engine_log.exists()
        assert ssh_log.exists()

        # 8. Unreachable SSH reports clear warning, keeps MacBook result, exits non-zero
        case = root / "offline-ssh"
        case.mkdir()
        engine, _ = make_mock_engine(case, sample_payload(needs_patch=0))
        ssh, _ = make_mock_ssh(case, reachable=False)
        res = run_script(
            [],
            {
                "AG_PATCH_OS": "Darwin",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode != 0
        assert "Не удалось подключиться" in res.stderr
        assert "✓ MacBook: готово" in res.stdout

        # 9. Running directly on Linux never invokes SSH
        case = root / "linux-local"
        case.mkdir()
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=1))
        ssh, ssh_log = make_mock_ssh(case, reachable=True)
        res = run_script(
            [],
            {
                "AG_PATCH_OS": "Linux",
                "AG_PATCH_ENGINE": str(engine),
                "AG_PATCH_SSH_BIN": str(ssh),
            },
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert not ssh_log.exists()
        assert "CALL:patch-files --prompt-close|PROMPT=1" in engine_log.read_text(encoding="utf-8")

        # 10. Symlink invocation resolves script & repo properly
        link = root / "ag-patch"
        link.symlink_to(SCRIPT)
        res = subprocess.run(
            [str(link), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res
        assert "ag-patch" in res.stdout and "--update" in res.stdout and "--check-updates" in res.stdout

        # 11. --check-updates and --update call updater script and forward flags over SSH
        case = root / "update-flow"
        case.mkdir()
        updater_log = case / "updater.log"
        mock_updater = write_executable(
            case / "mock-updater.py",
            f"""#!/usr/bin/env python3
import sys
with open({str(updater_log)!r}, "a", encoding="utf-8") as f:
    f.write("UPD:" + " ".join(sys.argv[1:]) + "\\n")
print("  ✓ MOCK UPDATER OK")
""",
        )
        engine, engine_log = make_mock_engine(case, sample_payload(needs_patch=1))
        ssh, ssh_log = make_mock_ssh(case, reachable=True, remote_rc=0)
        base_env = {
            "AG_PATCH_OS": "Darwin",
            "AG_PATCH_ENGINE": str(engine),
            "AG_PATCH_SSH_BIN": str(ssh),
            "AG_PATCH_UPDATER": str(mock_updater),
        }
        chk = run_script(["--check-updates"], base_env)
        assert chk.returncode == 0, chk.stdout + chk.stderr
        assert "--check" in updater_log.read_text(encoding="utf-8")
        assert not engine_log.exists()
        assert "--check-updates" in ssh_log.read_text(encoding="utf-8")

        updater_log.unlink()
        ssh_log.unlink()
        dry = run_script(["--update", "--dry-run"], base_env)
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert "--dry-run" in updater_log.read_text(encoding="utf-8")
        assert "patch-files" not in engine_log.read_text(encoding="utf-8")
        assert "--update" in ssh_log.read_text(encoding="utf-8") and "--dry-run" in ssh_log.read_text(encoding="utf-8")

        updater_log.unlink()
        engine_log.unlink()
        ssh_log.unlink()
        upd = run_script(["--update"], base_env)
        assert upd.returncode == 0, upd.stdout + upd.stderr
        assert "--apply" in updater_log.read_text(encoding="utf-8")
        assert "CALL:patch-files --prompt-close|PROMPT=1" in engine_log.read_text(encoding="utf-8")
        assert "--update" in ssh_log.read_text(encoding="utf-8")

    print("PASS: all ag_patch.sh offline scenarios passed (11/11)")


if __name__ == "__main__":
    main()
