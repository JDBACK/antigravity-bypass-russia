#!/usr/bin/env bash
# File-only CLI patching, status and rollback for Linux and macOS (including mise installs).
set -euo pipefail

os_name=$(uname -s)
case "$os_name" in
  Linux|Darwin) ;;
  *)
    printf 'Этот скрипт предназначен для Linux и macOS (обнаружено: %s).\n' "$os_name" >&2
    exit 1
    ;;
esac

usage() {
  cat <<EOF
Использование:
  $0 [ОПЦИИ] [путь к antigravity или agy]

Режимы:
  (по умолчанию)      Найти Antigravity CLI и применить файловый патч (patch-files)
  --all, all          Применить ко всем найденным файлам Antigravity (CLI, App, IDE)
  --restore, restore  Восстановить оригинальные файлы из бэкапа (restore-files)
  --status, status    Показать текущий статус патча CLI и системы
  --rebuild           Принудительно пересобрать Rust-движок перед запуском
  -h, --help          Показать эту справку
EOF
}

action="patch-files"
all_targets=0
rebuild=0
cli_path=""

while (( $# > 0 )); do
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
    --status|status)
      action="status"
      shift
      ;;
    --restore|restore)
      action="restore-files"
      shift
      ;;
    --patch|patch)
      action="patch-files"
      shift
      ;;
    --all|all)
      all_targets=1
      shift
      ;;
    --rebuild)
      rebuild=1
      shift
      ;;
    --)
      shift
      if (( $# > 1 )); then
        printf 'Лишний аргумент: %s\n' "$2" >&2
        usage >&2
        exit 2
      fi
      if (( $# == 1 )); then
        if [[ -n $cli_path ]]; then
          printf 'Лишний аргумент: %s\n' "$1" >&2
          usage >&2
          exit 2
        fi
        cli_path="$1"
      fi
      break
      ;;
    -*)
      printf 'Неизвестная опция: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n $cli_path ]]; then
        printf 'Лишний аргумент: %s\n' "$1" >&2
        usage >&2
        exit 2
      fi
      cli_path="$1"
      shift
      ;;
  esac
done

resolve_path() {
  local target="$1"
  local resolved=""
  if command -v realpath >/dev/null 2>&1; then
    if resolved=$(realpath -- "$target" 2>/dev/null); then
      printf '%s\n' "$resolved"
      return 0
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    if resolved=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$target" 2>/dev/null); then
      printf '%s\n' "$resolved"
      return 0
    fi
  fi
  local dir="" base="" link=""
  while [[ -L $target ]]; do
    dir=$(cd -- "$(dirname -- "$target")" && pwd -P)
    link=$(readlink "$target")
    if [[ $link == /* ]]; then
      target="$link"
    else
      target="$dir/$link"
    fi
  done
  if [[ -d $target ]]; then
    (cd -- "$target" && pwd -P)
  else
    dir=$(cd -- "$(dirname -- "$target")" && pwd -P)
    base=$(basename -- "$target")
    if [[ $dir == / ]]; then
      printf '/%s\n' "$base"
    else
      printf '%s/%s\n' "$dir" "$base"
    fi
  fi
}

resolve_user_home() {
  local home_dir="${HOME:-}"
  if [[ $(id -u) == 0 && -n ${SUDO_USER:-} ]]; then
    local detected=""
    case "$os_name" in
      Darwin)
        if command -v dscl >/dev/null 2>&1; then
          detected=$(dscl . -read "/Users/$SUDO_USER" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: //p' || true)
        fi
        ;;
      Linux)
        if command -v getent >/dev/null 2>&1; then
          detected=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)
        fi
        ;;
    esac
    if [[ -z $detected ]] && command -v python3 >/dev/null 2>&1; then
      detected=$(python3 -c 'import pwd, sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' "$SUDO_USER" 2>/dev/null || true)
    fi
    if [[ -n $detected ]]; then
      home_dir="$detected"
    fi
  fi
  printf '%s\n' "$home_dir"
}

resolve_mise_binary() {
  local mise_bin="$1"
  local name="$2"
  local resolved=""
  local candidate=""
  if [[ $(id -u) == 0 && ${SUDO_UID:-} =~ ^[1-9][0-9]*$ ]]; then
    candidate=$(sudo -H -u "#$SUDO_UID" -- "$mise_bin" which "$name" 2>/dev/null || true)
  else
    candidate=$("$mise_bin" which "$name" 2>/dev/null || true)
  fi
  if [[ -n $candidate && -f $candidate ]]; then
    resolved=$(resolve_path "$candidate")
    if [[ -f $resolved && $(basename -- "$resolved") != "mise" ]]; then
      printf '%s\n' "$resolved"
      return 0
    fi
  fi
  return 1
}

is_native_go_cli() {
  local file="$1"
  [[ -f $file ]] || return 1
  case "$os_name" in
    Darwin)
      if xcode-select -p >/dev/null 2>&1 && command -v otool >/dev/null 2>&1; then
        if otool -l "$file" 2>/dev/null | grep '__go_buildinfo' >/dev/null; then
          return 0
        fi
        return 1
      fi
      ;;
    Linux)
      if command -v readelf >/dev/null 2>&1; then
        if readelf -SW "$file" 2>/dev/null | grep '\.go\.buildinfo' >/dev/null; then
          return 0
        fi
        return 1
      fi
      ;;
  esac
  local magic=""
  magic=$(od -An -tx1 -N4 "$file" 2>/dev/null | tr -d ' \t\r\n' || true)
  case "$magic" in
    7f454c46|cffaedfe|feedfacf|cafebabe|bebafeca) return 0 ;;
    *) return 1 ;;
  esac
}

find_cargo() {
  local user_home="$1"
  local candidate=""
  candidate=$(command -v cargo 2>/dev/null || true)
  if [[ -n $candidate && -x $candidate ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  for candidate in \
    "${CARGO_HOME:-$user_home/.cargo}/bin/cargo" \
    "$user_home/.cargo/bin/cargo" \
    "/opt/homebrew/bin/cargo" \
    "/usr/local/bin/cargo"; do
    if [[ -x $candidate ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

script_path=$(resolve_path "${BASH_SOURCE[0]}")
repo_dir=$(dirname -- "$(dirname -- "$script_path")")
engine="$repo_dir/target/release/antigravity-bypass-russia"
user_home=$(resolve_user_home)

resolve_cli_path() {
  local candidate="${1:-}"
  local resolved=""
  local name=""

  if [[ -n $candidate ]]; then
    if [[ ! -f $candidate ]]; then
      printf 'Файл не найден: %s\n' "$candidate" >&2
      exit 1
    fi
    name=$(basename -- "$candidate")
    resolved=$(resolve_path "$candidate")
    if [[ $(basename -- "$resolved") == "mise" ]]; then
      if ! resolved=$(resolve_mise_binary "$resolved" "$name"); then
        printf 'Не удалось разрешить mise-shim: %s\n' "$candidate" >&2
        exit 1
      fi
    fi
    printf '%s\n' "$resolved"
    return 0
  fi

  for name in antigravity agy; do
    candidate=$(command -v "$name" 2>/dev/null || true)
    if [[ -n $candidate && -f $candidate ]]; then
      resolved=$(resolve_path "$candidate")
      if [[ $(basename -- "$resolved") == "mise" ]]; then
        resolved=$(resolve_mise_binary "$resolved" "$name" || true)
      fi
      if [[ -n $resolved ]] && is_native_go_cli "$resolved"; then
        printf '%s\n' "$resolved"
        return 0
      fi
    fi
  done

  local fallback_candidates=(
    "$user_home/.local/share/mise/installs/antigravity-cli/latest/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/latest/bin/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/latest/agy"
    "$user_home/.local/share/mise/installs/antigravity-cli/latest/bin/agy"
    "$user_home/.local/share/mise/installs/antigravity-cli/global/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/global/bin/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/global/agy"
    "$user_home/.local/share/mise/installs/antigravity-cli/global/bin/agy"
    "$user_home/.local/bin/antigravity"
    "$user_home/.local/bin/agy"
    "$user_home/.gemini/antigravity-cli/bin/antigravity"
    "$user_home/.gemini/antigravity-cli/bin/agy"
    "$user_home/.gemini/bin/antigravity"
    "$user_home/.gemini/bin/agy"
    "$user_home/.agy/bin/agy"
    "$user_home/.agy/bin/antigravity"
    "/opt/homebrew/bin/antigravity"
    "/opt/homebrew/bin/agy"
    "/usr/local/bin/antigravity"
    "/usr/local/bin/agy"
  )
  local item=""
  for item in "${fallback_candidates[@]}"; do
    if [[ -f $item ]]; then
      resolved=$(resolve_path "$item")
      if [[ $(basename -- "$resolved") != "mise" ]] && is_native_go_cli "$resolved"; then
        printf '%s\n' "$resolved"
        return 0
      fi
    fi
  done

  printf '%s\n' 'Antigravity CLI не найден. Передайте путь к исполняемому файлу аргументом.' >&2
  exit 1
}

needs_build() {
  if (( rebuild == 1 )) || [[ ! -x $engine ]]; then
    return 0
  fi
  local newer=""
  newer=$(find "$repo_dir/src" "$repo_dir/Cargo.toml" "$repo_dir/Cargo.lock" "$repo_dir/build.rs" -newer "$engine" -print 2>/dev/null || true)
  if [[ -n $newer ]]; then
    return 0
  fi
  return 1
}

if needs_build; then
  if ! cargo_bin=$(find_cargo "$user_home"); then
    printf '%s\n' 'Rust/Cargo не найден. Установите rustup (https://rustup.rs) и повторите запуск.' >&2
    exit 1
  fi
  cargo_dir=$(dirname -- "$cargo_bin")
  if [[ $(id -u) == 0 && ${SUDO_UID:-} =~ ^[1-9][0-9]*$ ]]; then
    sudo -H -u "#$SUDO_UID" -- env PATH="$cargo_dir:$PATH" "$cargo_bin" build --release --locked --manifest-path "$repo_dir/Cargo.toml"
  else
    PATH="$cargo_dir:$PATH" "$cargo_bin" build --release --locked --manifest-path "$repo_dir/Cargo.toml"
  fi
fi

if (( all_targets == 1 )) && [[ -n $cli_path ]]; then
  printf '%s\n' 'Нельзя одновременно указывать --all и конкретный путь.' >&2
  usage >&2
  exit 2
fi

if [[ $action == "status" ]]; then
  exec "$engine" status
fi

if [[ $action == "patch-files" && -t 2 && -z ${AGY_CLI_DISABLE_AUTO_UPDATE:-} ]]; then
  printf '%s\n' 'Совет: чтобы автообновление agy не затирало патч, задайте AGY_CLI_DISABLE_AUTO_UPDATE=1.' >&2
fi

if (( all_targets == 1 )); then
  exec "$engine" "$action"
fi

resolved_cli=$(resolve_cli_path "$cli_path")
exec "$engine" "$action" "$resolved_cli"
