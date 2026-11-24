"""Exercise launchers in temporary folders with a fake engine; never apply bypass."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ARGUMENTS = ["patch-files", "C:/Example Folder/путь", "--check", 'a"b', "", "C:\\Folder with space\\"]


def run(command):
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=90)


with tempfile.TemporaryDirectory(prefix="ag wrapper ") as temporary:
    folder = Path(temporary) / "download with space"
    folder.mkdir()
    source = folder / "probe.rs"
    source.write_text('fn main() { for a in std::env::args().skip(1) { println!("{}", a.as_bytes().iter().map(|b| format!("{:02x}", b)).collect::<String>()); } std::process::exit(7); }')
    binary = folder / ("antigravity-bypass-russia.exe" if os.name == "nt" else "antigravity-bypass-russia")
    subprocess.run(["rustc", str(source), "-o", str(binary)], check=True, timeout=90)

    # This older adjacent repository build must never override the downloaded engine.
    stale_dir = Path(temporary) / "target" / "release"
    stale_dir.mkdir(parents=True)
    stale_source = folder / "stale.rs"
    stale_source.write_text('fn main() { println!("stale engine"); std::process::exit(19); }')
    stale_binary = stale_dir / binary.name
    subprocess.run(["rustc", str(stale_source), "-o", str(stale_binary)], check=True, timeout=90)
    if os.name == "nt":
        shutil.copyfile(stale_binary, stale_dir / "antigravity-bypass-russia")

    if os.name == "nt":
        wrapper = folder / "unlock_and_restore.ps1"
        shutil.copyfile(ROOT / "scripts" / wrapper.name, wrapper)
        for shell_name in ["powershell.exe", "pwsh.exe"]:
            shell = shutil.which(shell_name)
            if not shell:
                print(f"SKIP: {shell_name} not installed")
                continue
            # Set console encoding explicitly for the test subprocess (PS 5.1 defaults to OEM).
            quote = lambda value: "'" + value.replace("'", "''") + "'"
            command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); & " + quote(str(wrapper))
            command += " " + " ".join(map(quote, ARGUMENTS)) + "; exit $LASTEXITCODE"
            result = run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command])
            assert result.returncode == 7, (shell_name, result.returncode, result.stdout, result.stderr)
            assert result.stdout.splitlines() == [a.encode("utf-8").hex() for a in ARGUMENTS], (shell_name, result.stdout)
            print(f"PASS: {shell_name} preserves arguments and failure exit code")

    bash = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
    if bash and Path(bash).is_file():
        wrapper = folder / "unlock_and_restore.sh"
        shutil.copyfile(ROOT / "scripts" / wrapper.name, wrapper)
        if os.name == "nt":
            shutil.copyfile(binary, folder / "antigravity-bypass-russia")
        elif os.uname().sysname == "Darwin":
            arch = "arm64" if os.uname().machine == "arm64" else "x64"
            shutil.copyfile(binary, folder / f"antigravity-bypass-russia-macos-{arch}")
            (folder / f"antigravity-bypass-russia-macos-{arch}").chmod(0o755)
        elif os.uname().sysname == "Linux":
            arch = "arm64" if os.uname().machine in ("aarch64", "arm64") else "x64"
            shutil.copyfile(binary, folder / f"antigravity-bypass-russia-linux-{arch}")
            (folder / f"antigravity-bypass-russia-linux-{arch}").chmod(0o755)
        result = run([bash, "-n", str(wrapper)])
        assert result.returncode == 0, result.stderr
        # Git Bash rewrites /-prefixed arguments for native programs unless disabled.
        os.environ["MSYS_NO_PATHCONV"] = "1"
        # Full POSIX quoting is verified with native engines on macOS CI.
        # Git Bash has an extra MSYS-to-Windows command-line translation layer.
        bash_arguments = ARGUMENTS[:3] if os.name == "nt" else ARGUMENTS
        result = run([bash, str(wrapper), *bash_arguments])
        assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)
        assert result.stdout.splitlines() == [a.encode("utf-8").hex() for a in bash_arguments], result.stdout
        print("PASS: Bash syntax, arguments and failure exit code")

        # Exercise source builds without a real Rust installation or sudo call.
        # BASH_ENV supplies the same macOS identity to the launcher and its child.
        repository = Path(temporary) / "source checkout with spaces"
        scripts = repository / "scripts"
        scripts.mkdir(parents=True)
        source_wrapper = scripts / "unlock_and_restore.sh"
        shutil.copyfile(ROOT / "scripts" / source_wrapper.name, source_wrapper)
        (repository / "Cargo.toml").write_text("# launcher fixture\n", encoding="utf-8")
        built_engine = repository / "target" / "release" / "antigravity-bypass-russia"
        built_engine.parent.mkdir(parents=True)
        engine_template = repository / "engine-template.sh"
        engine_template.write_bytes(b'#!/bin/bash\nprintf "%s\\n" "$@"\nexit 7\n')
        cargo_dir = repository / "rust toolchain" / "bin"
        cargo_dir.mkdir(parents=True)
        cargo_stub = cargo_dir / "cargo"
        cargo_stub.write_bytes('''#!/bin/bash
set -euo pipefail
[[ "$#" == 5 && "$1" == build && "$2" == --release && "$3" == --locked && "$4" == --manifest-path && "$5" == "$(cd -- "$ABR_TEST_REPOSITORY" && pwd)/Cargo.toml" ]] || exit 92
id -u > "$ABR_TEST_BUILD_UID"
rustc --version >/dev/null
if [[ "$ABR_TEST_BUILD_EXIT" != 0 ]]; then exit "$ABR_TEST_BUILD_EXIT"; fi
cp -- "$ABR_TEST_ENGINE" "$ABR_TEST_REPOSITORY/target/release/antigravity-bypass-russia"
chmod +x "$ABR_TEST_REPOSITORY/target/release/antigravity-bypass-russia"
'''.encode("utf-8"))
        cargo_stub.chmod(0o755)
        rustc_stub = cargo_dir / "rustc"
        rustc_stub.write_bytes(b'#!/bin/bash\nexit 0\n')
        rustc_stub.chmod(0o755)
        environment = repository / "test-environment.sh"
        environment.write_bytes('''uname() { if [[ "$1" == -s ]]; then echo Darwin; else echo arm64; fi; }
id() { if [[ "$1" == -u ]]; then echo "$ABR_TEST_UID"; else command id "$@"; fi; }
exec() {
  if [[ "$1" != /usr/bin/sudo ]]; then builtin exec "$@"; fi
  shift
  [[ "$1" == -H && "$2" == -u && "$3" == '#501' && "$4" == -- && "$5" == /bin/bash ]] || return 93
  printf '%s\\n' dropped > "$ABR_TEST_SUDO_MARKER"
  export ABR_TEST_UID=501
  unset SUDO_UID
  shift 4
  "$@"
  exit $?
}
'''.encode("utf-8"))
        build_uid = repository / "build-uid.txt"
        sudo_marker = repository / "sudo-used.txt"
        for mode, uid, sudo_uid, build_exit in [
            ("Cargo outside PATH", "501", "", "0"),
            ("old sudo invocation", "0", "501", "0"),
            ("failed build preserves failure", "501", "", "31"),
        ]:
            built_engine.write_bytes(b'#!/bin/bash\necho stale-engine\nexit 19\n')
            built_engine.chmod(0o755)
            sudo_marker.unlink(missing_ok=True)
            env = dict(os.environ, PATH="", BASH_ENV=environment.as_posix(),
                       CARGO_HOME=cargo_dir.parent.as_posix(), SUDO_UID=sudo_uid,
                       ABR_TEST_UID=uid, ABR_TEST_BUILD_EXIT=build_exit,
                       ABR_TEST_REPOSITORY=repository.as_posix(), ABR_TEST_ENGINE=engine_template.as_posix(),
                       ABR_TEST_BUILD_UID=build_uid.as_posix(), ABR_TEST_SUDO_MARKER=sudo_marker.as_posix())
            result = subprocess.run([bash, str(source_wrapper), *bash_arguments], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            assert result.returncode == (7 if build_exit == "0" else 31), (mode, result)
            assert result.stdout.splitlines() == (bash_arguments if build_exit == "0" else []), (mode, result)
            assert build_uid.read_text().strip() == "501", mode
            assert sudo_marker.exists() == (uid == "0"), mode
            print(f"PASS: source launcher: {mode}")

        if os.name != "nt":
            # Exercise the unified Linux/macOS CLI wrapper and its Linux compatibility alias.
            cli_repo = Path(temporary) / "cli checkout with spaces"
            cli_scripts = cli_repo / "scripts"
            cli_src = cli_repo / "src"
            cli_scripts.mkdir(parents=True)
            cli_src.mkdir(parents=True)
            (cli_repo / "Cargo.toml").write_text("# cli fixture\n", encoding="utf-8")
            (cli_repo / "Cargo.lock").write_text("# lock fixture\n", encoding="utf-8")
            (cli_repo / "build.rs").write_text("fn main() {}\n", encoding="utf-8")
            (cli_src / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            for name in ("patch_cli.sh", "patch_cli_linux.sh"):
                dest = cli_scripts / name
                shutil.copyfile(ROOT / "scripts" / name, dest)
                dest.chmod(0o755)
                syntax = run([bash, "-n", str(dest)])
                assert syntax.returncode == 0, (name, syntax.stderr)

            cli_engine = cli_repo / "target" / "release" / "antigravity-bypass-russia"
            cli_engine.parent.mkdir(parents=True)
            cli_cargo_dir = cli_repo / "toolchain" / "bin"
            cli_cargo_dir.mkdir(parents=True)
            cli_build_count = cli_repo / "build-count.txt"
            cli_build_count.write_text("0\n", encoding="utf-8")
            cli_cargo_stub = cli_cargo_dir / "cargo"
            cli_cargo_stub.write_bytes('''#!/bin/bash
set -euo pipefail
count=$(cat "$ABR_CLI_BUILD_COUNT")
printf '%s\\n' "$((count + 1))" > "$ABR_CLI_BUILD_COUNT"
cat > "$ABR_CLI_REPO/target/release/antigravity-bypass-russia" <<'ENGINE'
#!/bin/bash
printf '%s\\n' "$@"
exit 11
ENGINE
chmod +x "$ABR_CLI_REPO/target/release/antigravity-bypass-russia"
'''.encode("utf-8"))
            cli_cargo_stub.chmod(0o755)

            fake_bin = cli_repo / "fake-bin"
            fake_bin.mkdir()
            # Wrapper script named `antigravity` in PATH must be skipped in favor of native Go CLI.
            ide_wrapper = fake_bin / "antigravity"
            ide_wrapper.write_text("#!/bin/sh\necho ide wrapper\n", encoding="utf-8")
            ide_wrapper.chmod(0o755)

            real_cli = cli_repo / "real-install" / "agy"
            real_cli.parent.mkdir(parents=True)
            real_cli.write_bytes(b"\xcf\xfa\xed\xfeFAKE_GO_CLI")
            real_cli.chmod(0o755)
            mise_bin = fake_bin / "mise"
            mise_bin.write_bytes(f'''#!/bin/bash
if [[ "$1" == which && "$2" == agy ]]; then
  printf '%s\\n' "{real_cli.as_posix()}"
  exit 0
fi
exit 1
'''.encode("utf-8"))
            mise_bin.chmod(0o755)
            os.symlink(mise_bin, fake_bin / "agy")

            # Provide lightweight native-Go detector stubs matching the host OS tool.
            if os.uname().sysname == "Darwin":
                otool_stub = fake_bin / "otool"
                otool_stub.write_bytes(f'''#!/bin/bash
if [[ "$2" == "{real_cli.resolve().as_posix()}" ]]; then
  echo "  sectname __go_buildinfo"
  exit 0
fi
exit 1
'''.encode("utf-8"))
                otool_stub.chmod(0o755)
            else:
                readelf_stub = fake_bin / "readelf"
                readelf_stub.write_bytes(f'''#!/bin/bash
if [[ "$2" == "{real_cli.resolve().as_posix()}" ]]; then
  echo "  [12] .go.buildinfo PROGBITS"
  exit 0
fi
exit 1
'''.encode("utf-8"))
                readelf_stub.chmod(0o755)

            cli_env = dict(
                os.environ,
                PATH=f"{fake_bin.as_posix()}:{cli_cargo_dir.as_posix()}:{os.environ.get('PATH', '')}",
                HOME=(cli_repo / "empty-home").as_posix(),
                ABR_CLI_REPO=cli_repo.as_posix(),
                ABR_CLI_BUILD_COUNT=cli_build_count.as_posix(),
            )
            (cli_repo / "empty-home").mkdir()

            # 1) First run builds engine, skips IDE shell wrapper, resolves mise shim -> real_cli.
            res = subprocess.run(
                [bash, str(cli_scripts / "patch_cli.sh")],
                env=cli_env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            assert res.returncode == 11, (res.returncode, res.stdout, res.stderr)
            assert res.stdout.splitlines() == ["patch-files", str(real_cli.resolve())], res.stdout
            assert cli_build_count.read_text().strip() == "1"

            # 2) Second run (--status via compatibility alias) reuses built engine without rebuilding.
            res = subprocess.run(
                [bash, str(cli_scripts / "patch_cli_linux.sh"), "--status"],
                env=cli_env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            assert res.returncode == 11, (res.returncode, res.stdout, res.stderr)
            assert res.stdout.splitlines() == ["status"], res.stdout
            assert cli_build_count.read_text().strip() == "1"

            # 3) An older engine triggers incremental rebuild (verifies POSIX find -newer).
            past_mtime = (cli_src / "main.rs").stat().st_mtime - 10
            os.utime(cli_engine, (past_mtime, past_mtime))
            res = subprocess.run(
                [bash, str(cli_scripts / "patch_cli.sh"), "--restore"],
                env=cli_env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            assert res.returncode == 11, (res.returncode, res.stdout, res.stderr)
            assert res.stdout.splitlines() == ["restore-files", str(real_cli.resolve())], res.stdout
            assert cli_build_count.read_text().strip() == "2"

            # 4) Explicit path resolves symlinks and passes through cleanly.
            explicit_link = cli_repo / "explicit-link"
            os.symlink(real_cli, explicit_link)
            res = subprocess.run(
                [bash, str(cli_scripts / "patch_cli.sh"), str(explicit_link)],
                env=cli_env, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            assert res.returncode == 11, (res.returncode, res.stdout, res.stderr)
            assert res.stdout.splitlines() == ["patch-files", str(real_cli.resolve())], res.stdout
            assert cli_build_count.read_text().strip() == "2"
            print("PASS: unified patch_cli.sh and patch_cli_linux.sh wrapper checks")
    else:
        print("SKIP: Bash not installed")
