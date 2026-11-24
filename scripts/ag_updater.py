#!/usr/bin/env python3
"""Safe Antigravity updater with checksum and patch-compatibility staging checks."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

USER_AGENT = "antigravity-ag-updater/1.0"
HTTP_TIMEOUT = 120


def env_url(name: str, default: str) -> str:
    return os.environ.get(name, default).rstrip("/")


def http_get_bytes(url: str) -> tuple[bytes, dict]:
    encoded = urllib.parse.quote(url, safe=":/?&=%+#@-_.~")
    req = urllib.request.Request(encoded, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return data, headers


def http_download(url: str, dest: Path) -> dict:
    encoded = urllib.parse.quote(url, safe=":/?&=%+#@-_.~")
    req = urllib.request.Request(encoded, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
        return {k.lower(): v for k, v in resp.headers.items()}


def parse_version_tuple(version: str) -> tuple:
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def is_newer(latest: str, installed: str) -> bool:
    if not installed:
        return True
    return parse_version_tuple(latest) > parse_version_tuple(installed)


def parse_mac_updater_yaml(text: str) -> dict:
    info = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("version:") and "version" not in info:
            info["version"] = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("- url:") and "url" not in info:
            info["url"] = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("url:") and "url" not in info:
            info["url"] = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("sha512:") and "sha512" not in info:
            info["sha512"] = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("size:") and "size" not in info:
            try:
                info["size"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return info


def file_digest_bytes(path: Path, alg: str) -> bytes:
    h = hashlib.new(alg)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.digest()


def verify_sha512_base64(path: Path, expected_b64: str) -> None:
    expected = base64.b64decode(expected_b64)
    actual = file_digest_bytes(path, "sha512")
    if actual != expected:
        raise RuntimeError("Не совпала контрольная сумма SHA-512 (base64)")


def verify_sha512_hex(path: Path, expected_hex: str) -> None:
    actual = file_digest_bytes(path, "sha512").hex()
    if actual.lower() != expected_hex.lower():
        raise RuntimeError("Не совпала контрольная сумма SHA-512")


def verify_sha256_hex(path: Path, expected_hex: str) -> None:
    actual = file_digest_bytes(path, "sha256").hex()
    if actual.lower() != expected_hex.lower():
        raise RuntimeError("Не совпала контрольная сумма SHA-256")


def run_inspect(engine: Path, target_path: Path) -> dict:
    res = subprocess.run(
        [str(engine), "inspect-files", "--json", str(target_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"inspect-files завершился с кодом {res.returncode}: {res.stderr.strip()}")
    return json.loads(res.stdout)


def verify_staged_patch_compatible(engine: Path, staged_path: Path, label: str) -> None:
    data = run_inspect(engine, staged_path)
    targets = data.get("targets", [])
    if not targets:
        raise RuntimeError(f"{label}: в скачанном обновлении не найдено ни одного целевого файла для патча")
    unsupported = [t for t in targets if t.get("state") != "stock"]
    if unsupported:
        details = ", ".join(f"{t.get('name')} ({t.get('state')})" for t in unsupported)
        raise RuntimeError(
            f"{label}: патчер не поддерживает сигнатуру новой версии ({details}) — старая версия сохранена"
        )


def ancestor_pids() -> set:
    protected = {1, os.getpid()}
    pid = os.getpid()
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        protected.add(pid)
        try:
            if sys.platform.startswith("linux"):
                stat_txt = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                after = stat_txt.rsplit(") ", 1)[1].split()
                pid = int(after[1])
            else:
                out = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(pid)], text=True).strip()
                pid = int(out) if out else 1
        except Exception:
            break
    return protected


def extract_holder_pids(holders: list) -> list:
    pids = []
    for h in holders:
        m = re.search(r"\(PID (\d+)\)$", h)
        if m:
            pid = int(m.group(1))
            if pid not in pids:
                pids.append(pid)
    return pids


def ensure_closed_for_update(engine: Path, target_paths: list, label: str, app_name: str = "") -> bool:
    all_holders = []
    summaries = []
    for path in target_paths:
        if not path.exists():
            continue
        data = run_inspect(engine, path)
        for key in ("patch_holders", "restore_holders"):
            for item in data.get(key, []):
                if item not in all_holders:
                    all_holders.append(item)
        for key in ("patch_holders_summary", "restore_holders_summary"):
            s = data.get(key, "")
            if s and s not in summaries:
                summaries.append(s)

    if not all_holders:
        return True

    pids = extract_holder_pids(all_holders)
    protected = ancestor_pids()
    if any(pid in protected for pid in pids):
        print(
            f"  ! {label} запущен в родительской цепочке текущей сессии — закрыть его отсюда нельзя.\n"
            f"    Откройте отдельный терминал (Terminal / Ghostty) и повторите ag-patch --update.",
            file=sys.stderr,
        )
        return False

    summary = ", ".join(summaries) if summaries else ", ".join(all_holders)
    print(f"  ! Для обновления {label} нужно закрыть: {summary}")
    auto_close = os.environ.get("AG_UPDATER_AUTO_CLOSE") == "1"
    if not auto_close:
        if not sys.stdin.isatty():
            print(f"  ✗ {label} открыт, а терминал неинтерактивный — обновление пропущено.", file=sys.stderr)
            return False
        print(f"    [1] Закрыть {label} и обновить")
        print("    [0] Пропустить обновление этого компонента")
        while True:
            try:
                ans = input("    Выберите [1, 0]: ").strip()
            except EOFError:
                return False
            if ans == "0":
                print(f"  – Обновление {label} пропущено.")
                return False
            if ans == "1":
                break

    if app_name and sys.platform == "darwin":
        subprocess.run(
            ["/usr/bin/osascript", "-e", f'tell application "{app_name}" to quit'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        time.sleep(1.0)

    deadline = time.monotonic() + 6.0
    sent_term = set()
    sent_kill = set()
    while True:
        remaining = []
        for path in target_paths:
            if not path.exists():
                continue
            data = run_inspect(engine, path)
            for key in ("patch_holders", "restore_holders"):
                for item in data.get(key, []):
                    if item not in remaining:
                        remaining.append(item)
        if not remaining:
            print(f"  ✓ {label} закрыт")
            return True

        rem_pids = extract_holder_pids(remaining)
        if any(pid in protected for pid in rem_pids):
            print(f"  ✗ Нельзя закрыть родительский процесс текущей сессии.", file=sys.stderr)
            return False

        now = time.monotonic()
        for pid in rem_pids:
            try:
                if pid not in sent_term:
                    os.kill(pid, signal.SIGTERM)
                    sent_term.add(pid)
                elif now >= deadline - 2.0 and pid not in sent_kill:
                    os.kill(pid, signal.SIGKILL)
                    sent_kill.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError as err:
                print(f"  ✗ Не удалось завершить PID {pid}: {err}", file=sys.stderr)
                return False

        if now >= deadline:
            print(f"  ✗ {label} всё ещё открыт ({', '.join(remaining)}). Закройте вручную и повторите.", file=sys.stderr)
            return False
        time.sleep(0.25)


def extract_zip_preserving_mac_bundle(zip_path: Path, dest_dir: Path) -> None:
    if sys.platform == "darwin" and Path("/usr/bin/ditto").is_file():
        subprocess.run(["/usr/bin/ditto", "-xk", str(zip_path), str(dest_dir)], check=True, timeout=180)
    else:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                target = (dest_dir / member.filename).resolve()
                if not str(target).startswith(str(dest_dir.resolve())):
                    raise RuntimeError(f"Небезопасный путь в архиве: {member.filename}")
            zf.extractall(dest_dir)


def atomic_replace_app_bundle(staged_app: Path, dest_app: Path) -> None:
    parent = dest_app.parent
    pid = os.getpid()
    new_stage = parent / f".{dest_app.name}.new-{pid}"
    old_backup = parent / f".{dest_app.name}.old-{pid}"
    if new_stage.exists():
        shutil.rmtree(new_stage)
    if old_backup.exists():
        shutil.rmtree(old_backup)

    if sys.platform == "darwin" and Path("/usr/bin/ditto").is_file():
        subprocess.run(["/usr/bin/ditto", str(staged_app), str(new_stage)], check=True, timeout=180)
    else:
        shutil.copytree(staged_app, new_stage, symlinks=True)

    swapped_old = False
    try:
        if dest_app.exists():
            dest_app.rename(old_backup)
            swapped_old = True
        new_stage.rename(dest_app)
    except Exception:
        if swapped_old and not dest_app.exists() and old_backup.exists():
            old_backup.rename(dest_app)
        if new_stage.exists():
            shutil.rmtree(new_stage, ignore_errors=True)
        raise
    else:
        if old_backup.exists():
            shutil.rmtree(old_backup, ignore_errors=True)


def atomic_replace_binary(staged_bin: Path, dest_bin: Path) -> None:
    dest_bin.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest_bin.parent / f".{dest_bin.name}.update-{os.getpid()}"
    try:
        shutil.copy2(staged_bin, tmp_dest)
        tmp_dest.chmod(0o755)
        os.replace(tmp_dest, dest_bin)
    except Exception:
        if tmp_dest.exists():
            tmp_dest.unlink(missing_ok=True)
        raise


def cli_binary_version(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        res = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15)
        m = re.search(r"\d+\.\d+\.\d+", (res.stdout or "") + " " + (res.stderr or ""))
        return m.group(0) if m else ""
    except Exception:
        return ""


def fetch_latest_app_mac(arch_tag: str) -> dict:
    hub_base = env_url("AG_UPDATER_HUB_BASE", "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app")
    body, _ = http_get_bytes(f"{hub_base}/manifest/latest-{arch_tag}-mac.yml")
    info = parse_mac_updater_yaml(body.decode("utf-8"))
    if not info.get("version") or not info.get("url") or not info.get("sha512"):
        raise RuntimeError("Некорректный манифест Antigravity App")
    return info


def fetch_latest_ide_mac(arch_tag: str) -> dict:
    ide_base = env_url("AG_UPDATER_IDE_BASE", "https://antigravity-ide-auto-updater-974169037036.us-central1.run.app")
    body, _ = http_get_bytes(f"{ide_base}/api/update/darwin-{arch_tag}/stable/latest")
    data = json.loads(body.decode("utf-8"))
    url = data.get("url", "")
    sha256 = data.get("sha256hash", "")
    m = re.search(r"/stable/(\d+\.\d+\.\d+)-", url)
    version = m.group(1) if m else str(data.get("productVersion", ""))
    if not version or not url or not sha256:
        raise RuntimeError("Некорректный ответ API обновлений Antigravity IDE")
    return {"version": version, "url": url, "sha256": sha256}


def fetch_latest_cli(platform_key: str) -> dict:
    cli_base = env_url("AG_UPDATER_CLI_BASE", "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app")
    body, _ = http_get_bytes(f"{cli_base}/manifests/{platform_key}.json")
    data = json.loads(body.decode("utf-8"))
    for key in ("version", "url", "sha512"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise RuntimeError(f"Некорректный манифест CLI ({platform_key}): отсутствует {key}")
    return data


def fetch_latest_hub_server(arch_tag: str) -> dict:
    hub_base = env_url("AG_UPDATER_HUB_BASE", "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app")
    pub_base = env_url("AG_UPDATER_PUBLIC_BASE", "https://storage.googleapis.com/antigravity-public")
    body, _ = http_get_bytes(f"{hub_base}/releases")
    releases = json.loads(body.decode("utf-8"))
    if not isinstance(releases, list) or not releases:
        raise RuntimeError("Пустой список релизов Antigravity Hub Server")
    latest = releases[0]
    version = str(latest.get("version", ""))
    exec_id = str(latest.get("execution_id", ""))
    if not version or not exec_id:
        raise RuntimeError("Некорректная запись релиза Antigravity Hub Server")
    url = f"{pub_base}/antigravity-hub/{version}-{exec_id}/unsigned/language_server-linux-{arch_tag}"
    return {"version": version, "execution_id": exec_id, "url": url}


def extract_cli_from_tar(archive_path: Path, dest_bin: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as tar:
        members = [
            m for m in tar.getmembers()
            if m.isfile() and not m.issym() and not m.islnk() and Path(m.name).name in {"agy", "antigravity"}
        ]
        if len(members) != 1:
            raise RuntimeError(f"Ожидался ровно один бинарник CLI в архиве, найдено: {len(members)}")
        with tar.extractfile(members[0]) as src, dest_bin.open("wb") as out:
            shutil.copyfileobj(src, out)
    dest_bin.chmod(0o755)


def installed_app_version(app_path: Path) -> str:
    plist_path = app_path / "Contents/Info.plist"
    if not plist_path.is_file():
        return ""
    with plist_path.open("rb") as f:
        data = plistlib.load(f)
    return str(data.get("CFBundleShortVersionString", ""))


def installed_ide_version(ide_path: Path) -> str:
    product_json = ide_path / "Contents/Resources/app/product.json"
    if not product_json.is_file():
        return ""
    data = json.loads(product_json.read_text(encoding="utf-8"))
    return str(data.get("ideVersion") or data.get("version") or "")


def installed_hub_server_versions(home: Path, arch_tag: str) -> list:
    bin_dir = home / ".antigravity-hub-server/bin"
    if not bin_dir.is_dir():
        return []
    suffix = f"-{arch_tag}"
    versions = []
    for entry in bin_dir.iterdir():
        if entry.is_dir() and entry.name.endswith(suffix) and (entry / "language_server").is_file():
            versions.append(entry.name[: -len(suffix)])
    versions.sort(key=parse_version_tuple)
    return versions


def find_mac_cli_paths(home: Path) -> list:
    candidates = [home / ".local/bin/agy", home / ".gemini/bin/agy"]
    found = []
    seen_real = set()
    for p in candidates:
        if p.is_file() and not p.is_symlink():
            real = p.resolve()
            if real not in seen_real:
                seen_real.add(real)
                found.append(p)
    return found


def find_linux_cli_info(home: Path) -> tuple[bool, Path | None]:
    mise_root = home / ".local/share/mise/installs/antigravity-cli"
    mise_latest = mise_root / "latest/antigravity"
    if mise_latest.is_file():
        return True, mise_latest.resolve()
    for candidate in (home / ".local/bin/agy", home / ".local/bin/antigravity"):
        if candidate.is_file():
            return False, candidate.resolve()
    return False, None


def handle_mac(engine: Path, mode: str, arch: str, home: Path, apps_dir: Path) -> int:
    arch_tag = "arm64" if arch in ("arm64", "aarch64") else "x64"
    cli_platform = "darwin_arm64" if arch_tag == "arm64" else "darwin_amd64"
    had_error = False

    # 1. Antigravity Desktop App
    app_path = apps_dir / "Antigravity.app"
    if app_path.is_dir():
        try:
            cur = installed_app_version(app_path)
            latest = fetch_latest_app_mac(arch_tag)
            if not is_newer(latest["version"], cur):
                print(f"  ✓ Antigravity App: {cur} (актуально)")
            elif mode in ("check", "dry-run"):
                print(f"  ↑ Antigravity App: {cur} -> {latest['version']} (доступно обновление)")
            else:
                print(f"  ↓ Antigravity App: обновляю {cur} -> {latest['version']}...")
                with tempfile.TemporaryDirectory(prefix="ag-upd-app-") as td:
                    tmp = Path(td)
                    archive = tmp / "Antigravity.zip"
                    http_download(latest["url"], archive)
                    verify_sha512_base64(archive, latest["sha512"])
                    if latest.get("size") and archive.stat().st_size != latest["size"]:
                        raise RuntimeError("Размер Antigravity.zip не совпал с манифестом")
                    extract_dir = tmp / "extract"
                    extract_dir.mkdir()
                    extract_zip_preserving_mac_bundle(archive, extract_dir)
                    staged = extract_dir / "Antigravity.app"
                    if not staged.is_dir():
                        raise RuntimeError("В архиве не найден Antigravity.app")
                    verify_staged_patch_compatible(engine, staged, f"Antigravity App {latest['version']}")
                    if ensure_closed_for_update(engine, [app_path], "Antigravity", app_name="Antigravity"):
                        atomic_replace_app_bundle(staged, app_path)
                        print(f"  ✓ Antigravity App обновлён до {latest['version']} (патч проверен, будет применён далее)")
                    else:
                        had_error = True
        except Exception as err:
            print(f"  ✗ Antigravity App: {err}", file=sys.stderr)
            had_error = True

    # 2. Antigravity IDE
    ide_path = apps_dir / "Antigravity IDE.app"
    if ide_path.is_dir():
        try:
            cur = installed_ide_version(ide_path)
            latest = fetch_latest_ide_mac(arch_tag)
            if not is_newer(latest["version"], cur):
                print(f"  ✓ Antigravity IDE: {cur} (актуально)")
            elif mode in ("check", "dry-run"):
                print(f"  ↑ Antigravity IDE: {cur} -> {latest['version']} (доступно обновление)")
            else:
                print(f"  ↓ Antigravity IDE: обновляю {cur} -> {latest['version']}...")
                with tempfile.TemporaryDirectory(prefix="ag-upd-ide-") as td:
                    tmp = Path(td)
                    archive = tmp / "Antigravity_IDE.zip"
                    http_download(latest["url"], archive)
                    verify_sha256_hex(archive, latest["sha256"])
                    extract_dir = tmp / "extract"
                    extract_dir.mkdir()
                    extract_zip_preserving_mac_bundle(archive, extract_dir)
                    staged = extract_dir / "Antigravity IDE.app"
                    if not staged.is_dir():
                        raise RuntimeError("В архиве не найден Antigravity IDE.app")
                    verify_staged_patch_compatible(engine, staged, f"Antigravity IDE {latest['version']}")
                    if ensure_closed_for_update(engine, [ide_path], "Antigravity IDE", app_name="Antigravity IDE"):
                        atomic_replace_app_bundle(staged, ide_path)
                        print(f"  ✓ Antigravity IDE обновлён до {latest['version']} (патч проверен, будет применён далее)")
                    else:
                        had_error = True
        except Exception as err:
            print(f"  ✗ Antigravity IDE: {err}", file=sys.stderr)
            had_error = True

    # 3. Antigravity CLI (macOS)
    cli_paths = find_mac_cli_paths(home)
    if cli_paths:
        try:
            latest = fetch_latest_cli(cli_platform)
            versions = {p: cli_binary_version(p) for p in cli_paths}
            outdated = [p for p, v in versions.items() if is_newer(latest["version"], v)]
            if not outdated:
                cur_label = versions[cli_paths[0]] or "?"
                print(f"  ✓ Antigravity CLI: {cur_label} (актуально)")
            elif mode in ("check", "dry-run"):
                cur_label = versions[outdated[0]] or "?"
                print(f"  ↑ Antigravity CLI: {cur_label} -> {latest['version']} (доступно обновление)")
            else:
                cur_label = versions[outdated[0]] or "?"
                print(f"  ↓ Antigravity CLI: обновляю {cur_label} -> {latest['version']}...")
                with tempfile.TemporaryDirectory(prefix="ag-upd-cli-") as td:
                    tmp = Path(td)
                    archive = tmp / "cli.tar.gz"
                    http_download(latest["url"], archive)
                    verify_sha512_hex(archive, latest["sha512"])
                    staged_cli = tmp / "agy"
                    extract_cli_from_tar(archive, staged_cli)
                    verify_staged_patch_compatible(engine, staged_cli, f"Antigravity CLI {latest['version']}")
                    if ensure_closed_for_update(engine, outdated, "Antigravity CLI"):
                        for dest in outdated:
                            atomic_replace_binary(staged_cli, dest)
                        print(f"  ✓ Antigravity CLI обновлён до {latest['version']} (патч проверен, будет применён далее)")
                    else:
                        had_error = True
        except Exception as err:
            print(f"  ✗ Antigravity CLI: {err}", file=sys.stderr)
            had_error = True

    return 1 if had_error else 0


def handle_linux(engine: Path, mode: str, arch: str, home: Path) -> int:
    arch_tag = "arm64" if arch in ("arm64", "aarch64") else "x64"
    cli_platform = "linux_amd64"
    had_error = False

    # 1. Antigravity Hub Server (~/.antigravity-hub-server/bin/<ver>-<arch>/language_server)
    hub_root = home / ".antigravity-hub-server"
    if hub_root.is_dir():
        try:
            installed_vers = installed_hub_server_versions(home, arch_tag)
            cur = installed_vers[-1] if installed_vers else ""
            latest = fetch_latest_hub_server(arch_tag)
            dest_bin = hub_root / "bin" / f"{latest['version']}-{arch_tag}" / "language_server"
            if dest_bin.is_file() or (cur and not is_newer(latest["version"], cur)):
                print(f"  ✓ Antigravity Hub Server: {cur or latest['version']} (актуально)")
            elif mode in ("check", "dry-run"):
                print(f"  ↑ Antigravity Hub Server: {cur or 'не установлен'} -> {latest['version']} (доступно обновление)")
            else:
                print(f"  ↓ Antigravity Hub Server: скачиваю {latest['version']} заранее...")
                with tempfile.TemporaryDirectory(prefix="ag-upd-hub-") as td:
                    staged_ls = Path(td) / "language_server"
                    headers = http_download(latest["url"], staged_ls)
                    expected_len = headers.get("content-length")
                    if expected_len and staged_ls.stat().st_size != int(expected_len):
                        raise RuntimeError("Размер language_server не совпал с Content-Length")
                    with staged_ls.open("rb") as f:
                        if f.read(4) != b"\x7fELF":
                            raise RuntimeError("Скачанный language_server не является ELF-файлом")
                    staged_ls.chmod(0o755)
                    verify_staged_patch_compatible(engine, staged_ls, f"Antigravity Hub Server {latest['version']}")
                    atomic_replace_binary(staged_ls, dest_bin)
                    print(
                        f"  ✓ Antigravity Hub Server {latest['version']} установлен в {dest_bin} "
                        "(патч проверен, будет применён далее)"
                    )
        except Exception as err:
            print(f"  ✗ Antigravity Hub Server: {err}", file=sys.stderr)
            had_error = True

    # 2. Antigravity CLI (Linux x64)
    is_mise, cli_path = find_linux_cli_info(home)
    if cli_path is not None:
        try:
            cur = cli_binary_version(cli_path)
            latest = fetch_latest_cli(cli_platform)
            if not is_newer(latest["version"], cur):
                print(f"  ✓ Antigravity CLI: {cur} (актуально)")
            elif mode in ("check", "dry-run"):
                via = "через mise" if is_mise else "напрямую"
                print(f"  ↑ Antigravity CLI: {cur} -> {latest['version']} (доступно обновление {via})")
            else:
                print(f"  ↓ Antigravity CLI: проверяю и обновляю {cur} -> {latest['version']}...")
                with tempfile.TemporaryDirectory(prefix="ag-upd-cli-") as td:
                    tmp = Path(td)
                    archive = tmp / "cli.tar.gz"
                    http_download(latest["url"], archive)
                    verify_sha512_hex(archive, latest["sha512"])
                    staged_cli = tmp / "antigravity"
                    extract_cli_from_tar(archive, staged_cli)
                    verify_staged_patch_compatible(engine, staged_cli, f"Antigravity CLI {latest['version']}")
                    if is_mise:
                        mise_bin = os.environ.get("AG_UPDATER_MISE_BIN") or shutil.which("mise")
                        if not mise_bin:
                            raise RuntimeError("mise не найден в PATH для обновления antigravity-cli")
                        subprocess.run([mise_bin, "upgrade", "antigravity-cli"], check=True, timeout=300)
                    else:
                        if not ensure_closed_for_update(engine, [cli_path], "Antigravity CLI"):
                            had_error = True
                            continue_install = False
                        else:
                            atomic_replace_binary(staged_cli, cli_path)
                            continue_install = True
                        if not continue_install:
                            return 1
                    print(f"  ✓ Antigravity CLI обновлён до {latest['version']} (патч проверен, будет применён далее)")
        except Exception as err:
            print(f"  ✗ Antigravity CLI: {err}", file=sys.stderr)
            had_error = True

    return 1 if had_error else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True, help="Path to antigravity-bypass-russia binary")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Only check installed vs latest versions")
    group.add_argument("--dry-run", action="store_true", help="Show planned updates without downloading")
    group.add_argument("--apply", action="store_true", help="Download, stage-verify, and install updates")
    parser.add_argument("--os", dest="os_name", default=os.environ.get("AG_PATCH_OS") or platform.system())
    parser.add_argument("--arch", default=platform.machine())
    args = parser.parse_args()

    mode = "check" if args.check else ("dry-run" if args.dry_run else "apply")
    home = Path(os.environ.get("AG_UPDATER_HOME", str(Path.home())))
    apps_dir = Path(os.environ.get("AG_UPDATER_APPS_DIR", "/Applications"))

    if args.os_name == "Darwin":
        return handle_mac(args.engine, mode, args.arch, home, apps_dir)
    if args.os_name == "Linux":
        return handle_linux(args.engine, mode, args.arch, home)

    print(f"  ! ОС не поддерживается апдейтером: {args.os_name}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
