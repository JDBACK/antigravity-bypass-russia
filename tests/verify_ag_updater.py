"""Offline regression tests for scripts/ag_updater.py using a local HTTP mock server."""

from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts/ag_updater.py"


def write_executable(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def make_targz_bytes(member_name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name=member_name)
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_plist_bytes(version: str) -> bytes:
    return plistlib.dumps({"CFBundleShortVersionString": version})


def start_mock_server(routes: dict[str, bytes]) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import unquote
            key = unquote(self.path)
            if key not in routes:
                self.send_response(404)
                self.end_headers()
                return
            payload = routes[key]
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def make_mock_engine(directory: Path, staged_state: str = "stock", holders: list[str] | None = None) -> Path:
    holders = holders or []
    return write_executable(
        directory / "mock-engine",
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path

assert sys.argv[1] == "inspect-files" and sys.argv[2] == "--json"
target = Path(sys.argv[3])
is_staged = "ag-upd-" in str(target)
state = {staged_state!r} if is_staged else "patched"
active_holders = [] if is_staged else {holders!r}
print(json.dumps({{
    "targets": [{{
        "kind": "antigravity",
        "label": "Antigravity",
        "name": target.name,
        "path": str(target),
        "state": state,
        "needs_patch": state != "patched",
        "needs_restore": state != "stock",
        "writable": True,
    }}],
    "needs_patch_count": 1 if state != "patched" else 0,
    "needs_restore_count": 1 if state != "stock" else 0,
    "needs_admin_for_patch": False,
    "needs_admin_for_restore": False,
    "patch_holders": active_holders,
    "patch_holders_summary": ", ".join(active_holders),
    "restore_holders": active_holders,
    "restore_holders_summary": ", ".join(active_holders),
    "holders_error": None,
}}))
""",
    )


def run_updater(engine: Path, args: list[str], env_overrides: dict[str, str]):
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(UPDATER), "--engine", str(engine), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def build_mac_fixtures(base_url: str, app_ver="2.36.0", ide_ver="2.9.0", cli_ver="1.5.17", bad_app_sha=False):
    app_zip = make_zip_bytes({
        "Antigravity.app/Contents/Info.plist": make_plist_bytes(app_ver),
        "Antigravity.app/Contents/Resources/bin/language_server": b"NEW_APP_LS_" + app_ver.encode(),
    })
    app_sha_b64 = base64.b64encode(hashlib.sha512(b"corrupt" if bad_app_sha else app_zip).digest()).decode()
    app_yaml = (
        f"version: {app_ver}\n"
        f"files:\n"
        f"  - url: {base_url}/antigravity-hub/{app_ver}-999/darwin-arm/Antigravity.zip\n"
        f"    sha512: {app_sha_b64}\n"
        f"    size: {len(app_zip)}\n"
    ).encode()

    ide_zip = make_zip_bytes({
        "Antigravity IDE.app/Contents/Resources/app/product.json": json.dumps(
            {"applicationName": "antigravity-ide", "ideVersion": ide_ver}
        ).encode(),
        "Antigravity IDE.app/Contents/Resources/app/out/main.js": b"// NEW IDE " + ide_ver.encode(),
    })
    ide_sha = hashlib.sha256(ide_zip).hexdigest()
    ide_json = json.dumps({
        "url": f"{base_url}/antigravity/stable/{ide_ver}-888/darwin-arm/Antigravity IDE.zip",
        "sha256hash": ide_sha,
        "productVersion": "1.108.0",
    }).encode()

    cli_bin = f"#!/bin/sh\necho {cli_ver}\n".encode()
    cli_tar = make_targz_bytes("agy", cli_bin)
    cli_sha = hashlib.sha512(cli_tar).hexdigest()
    cli_json = json.dumps({
        "version": cli_ver,
        "url": f"{base_url}/antigravity-cli/{cli_ver}-777/darwin-arm/cli_mac_arm64.tar.gz",
        "sha512": cli_sha,
    }).encode()

    return {
        "/manifest/latest-arm64-mac.yml": app_yaml,
        f"/antigravity-hub/{app_ver}-999/darwin-arm/Antigravity.zip": app_zip,
        "/api/update/darwin-arm64/stable/latest": ide_json,
        f"/antigravity/stable/{ide_ver}-888/darwin-arm/Antigravity IDE.zip": ide_zip,
        "/manifests/darwin_arm64.json": cli_json,
        f"/antigravity-cli/{cli_ver}-777/darwin-arm/cli_mac_arm64.tar.gz": cli_tar,
    }


def setup_mac_install(home: Path, apps: Path, app_ver="2.35.0", ide_ver="2.8.0", cli_ver="1.5.16"):
    (apps / "Antigravity.app/Contents/Resources/bin").mkdir(parents=True)
    (apps / "Antigravity.app/Contents/Info.plist").write_bytes(make_plist_bytes(app_ver))
    (apps / "Antigravity.app/Contents/Resources/bin/language_server").write_bytes(b"OLD_APP_LS")

    (apps / "Antigravity IDE.app/Contents/Resources/app/out").mkdir(parents=True)
    (apps / "Antigravity IDE.app/Contents/Resources/app/product.json").write_text(
        json.dumps({"applicationName": "antigravity-ide", "ideVersion": ide_ver}), encoding="utf-8"
    )
    (apps / "Antigravity IDE.app/Contents/Resources/app/out/main.js").write_text("// OLD IDE", encoding="utf-8")

    write_executable(home / ".local/bin/agy", f"#!/bin/sh\necho {cli_ver}\n")


def main():
    if sys.platform == "win32":
        print("SKIP: ag_updater.py tests are Unix-only")
        return

    with tempfile.TemporaryDirectory(prefix="ag-updater-test-") as tmp:
        root = Path(tmp)

        # 1. macOS: --check detects updates without modifying files, then --apply updates App + IDE + CLI atomically
        case = root / "mac-apply"
        home, apps = case / "home", case / "apps"
        setup_mac_install(home, apps)
        engine = make_mock_engine(case, staged_state="stock")
        routes = {}
        server, base = start_mock_server(routes)
        try:
            routes.update(build_mac_fixtures(base))
            env = {
                "AG_UPDATER_HUB_BASE": base,
                "AG_UPDATER_IDE_BASE": base,
                "AG_UPDATER_CLI_BASE": base,
                "AG_UPDATER_HOME": str(home),
                "AG_UPDATER_APPS_DIR": str(apps),
            }
            chk = run_updater(engine, ["--check", "--os", "Darwin", "--arch", "arm64"], env)
            assert chk.returncode == 0, chk.stdout + chk.stderr
            assert "Antigravity App: 2.35.0 -> 2.36.0" in chk.stdout
            assert "Antigravity IDE: 2.8.0 -> 2.9.0" in chk.stdout
            assert "Antigravity CLI: 1.5.16 -> 1.5.17" in chk.stdout
            assert (apps / "Antigravity.app/Contents/Resources/bin/language_server").read_bytes() == b"OLD_APP_LS"

            app_res = run_updater(engine, ["--apply", "--os", "Darwin", "--arch", "arm64"], env)
            assert app_res.returncode == 0, app_res.stdout + app_res.stderr
            assert b"NEW_APP_LS_2.36.0" == (apps / "Antigravity.app/Contents/Resources/bin/language_server").read_bytes()
            assert "NEW IDE 2.9.0" in (apps / "Antigravity IDE.app/Contents/Resources/app/out/main.js").read_text()
            assert "1.5.17" in subprocess.check_output([str(home / ".local/bin/agy"), "--version"], text=True)

            # Second --check reports everything up to date
            chk2 = run_updater(engine, ["--check", "--os", "Darwin", "--arch", "arm64"], env)
            assert chk2.returncode == 0, chk2.stdout + chk2.stderr
            assert "актуально" in chk2.stdout and "->" not in chk2.stdout
        finally:
            server.shutdown()

        # 2. Unsupported signature on staged update -> refuses update & preserves working old versions!
        case = root / "mac-unsupported"
        home, apps = case / "home", case / "apps"
        setup_mac_install(home, apps)
        engine = make_mock_engine(case, staged_state="unknown")
        routes = {}
        server, base = start_mock_server(routes)
        try:
            routes.update(build_mac_fixtures(base))
            env = {
                "AG_UPDATER_HUB_BASE": base,
                "AG_UPDATER_IDE_BASE": base,
                "AG_UPDATER_CLI_BASE": base,
                "AG_UPDATER_HOME": str(home),
                "AG_UPDATER_APPS_DIR": str(apps),
            }
            res = run_updater(engine, ["--apply", "--os", "Darwin", "--arch", "arm64"], env)
            assert res.returncode != 0
            assert "патчер не поддерживает сигнатуру" in res.stderr
            assert (apps / "Antigravity.app/Contents/Resources/bin/language_server").read_bytes() == b"OLD_APP_LS"
            assert "1.5.16" in subprocess.check_output([str(home / ".local/bin/agy"), "--version"], text=True)
        finally:
            server.shutdown()

        # 3. Corrupted checksum -> refuses update & preserves old app
        case = root / "mac-bad-sha"
        home, apps = case / "home", case / "apps"
        setup_mac_install(home, apps)
        engine = make_mock_engine(case, staged_state="stock")
        routes = {}
        server, base = start_mock_server(routes)
        try:
            routes.update(build_mac_fixtures(base, bad_app_sha=True))
            env = {
                "AG_UPDATER_HUB_BASE": base,
                "AG_UPDATER_IDE_BASE": base,
                "AG_UPDATER_CLI_BASE": base,
                "AG_UPDATER_HOME": str(home),
                "AG_UPDATER_APPS_DIR": str(apps),
            }
            res = run_updater(engine, ["--apply", "--os", "Darwin", "--arch", "arm64"], env)
            assert res.returncode != 0
            assert "контрольная сумма SHA-512" in res.stderr
            assert (apps / "Antigravity.app/Contents/Resources/bin/language_server").read_bytes() == b"OLD_APP_LS"
        finally:
            server.shutdown()

        # 4. Ancestor protection -> if current PID is reported as holder, refuses to kill self or overwrite
        case = root / "ancestor-guard"
        home, apps = case / "home", case / "apps"
        setup_mac_install(home, apps)
        engine = make_mock_engine(case, staged_state="stock", holders=[f"agy (PID {os.getpid()})"])
        routes = {}
        server, base = start_mock_server(routes)
        try:
            routes.update(build_mac_fixtures(base))
            env = {
                "AG_UPDATER_HUB_BASE": base,
                "AG_UPDATER_IDE_BASE": base,
                "AG_UPDATER_CLI_BASE": base,
                "AG_UPDATER_HOME": str(home),
                "AG_UPDATER_APPS_DIR": str(apps),
                "AG_UPDATER_AUTO_CLOSE": "1",
            }
            res = run_updater(engine, ["--apply", "--os", "Darwin", "--arch", "arm64"], env)
            assert res.returncode != 0
            assert "родительской цепочке" in res.stderr
            assert (apps / "Antigravity.app/Contents/Resources/bin/language_server").read_bytes() == b"OLD_APP_LS"
        finally:
            server.shutdown()

        # 5. Linux: Hub Server pre-stages standalone ELF binary + mise CLI upgrades only after patch-compat check
        case = root / "linux-apply"
        home = case / "home"
        (home / ".antigravity-hub-server/bin/2.35.0-x64").mkdir(parents=True)
        (home / ".antigravity-hub-server/bin/2.35.0-x64/language_server").write_bytes(b"\x7fELFOLD")
        mise_cli = home / ".local/share/mise/installs/antigravity-cli/1.5.16/antigravity"
        write_executable(mise_cli, "#!/bin/sh\necho 1.5.16\n")
        (home / ".local/share/mise/installs/antigravity-cli/latest").symlink_to("1.5.16")

        mise_log = case / "mise.log"
        mock_mise = write_executable(
            case / "mock-mise",
            f"#!/bin/sh\necho \"MISE:$*\" >> {str(mise_log)!r}\n",
        )

        engine = make_mock_engine(case, staged_state="stock")
        routes = {}
        server, base = start_mock_server(routes)
        try:
            ls_bytes = b"\x7fELFNEW_HUB_2.36.0"
            cli_bytes = b"#!/bin/sh\necho 1.5.17\n"
            cli_tar = make_targz_bytes("antigravity", cli_bytes)
            routes.update({
                "/releases": json.dumps([{"version": "2.36.0", "execution_id": "12345"}]).encode(),
                "/antigravity-hub/2.36.0-12345/unsigned/language_server-linux-x64": ls_bytes,
                "/manifests/linux_amd64.json": json.dumps({
                    "version": "1.5.17",
                    "url": f"{base}/antigravity-cli/1.5.17-555/linux-x64/cli_linux_x64.tar.gz",
                    "sha512": hashlib.sha512(cli_tar).hexdigest(),
                }).encode(),
                "/antigravity-cli/1.5.17-555/linux-x64/cli_linux_x64.tar.gz": cli_tar,
            })
            env = {
                "AG_UPDATER_HUB_BASE": base,
                "AG_UPDATER_PUBLIC_BASE": base,
                "AG_UPDATER_CLI_BASE": base,
                "AG_UPDATER_HOME": str(home),
                "AG_UPDATER_MISE_BIN": str(mock_mise),
            }
            chk = run_updater(engine, ["--check", "--os", "Linux", "--arch", "x86_64"], env)
            assert chk.returncode == 0, chk.stdout + chk.stderr
            assert "Antigravity Hub Server: 2.35.0 -> 2.36.0" in chk.stdout
            assert "Antigravity CLI: 1.5.16 -> 1.5.17" in chk.stdout

            res = run_updater(engine, ["--apply", "--os", "Linux", "--arch", "x86_64"], env)
            assert res.returncode == 0, res.stdout + res.stderr
            new_ls = home / ".antigravity-hub-server/bin/2.36.0-x64/language_server"
            assert new_ls.is_file() and new_ls.read_bytes() == ls_bytes
            assert (home / ".antigravity-hub-server/bin/2.35.0-x64/language_server").read_bytes() == b"\x7fELFOLD"
            assert "MISE:upgrade antigravity-cli" in mise_log.read_text(encoding="utf-8")
        finally:
            server.shutdown()

    print("PASS: all ag_updater.py offline scenarios passed (5/5)")


if __name__ == "__main__":
    main()
