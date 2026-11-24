#!/usr/bin/env bash
# File-only CLI patching, status and rollback for Linux (including mise installs).
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
  printf '%s\n' 'Этот скрипт предназначен для Linux.' >&2
  exit 1
fi

usage() {
  cat <<EOF
Использование:
  $0 [ОПЦИИ] [путь к antigravity или agy]

Режимы:
  (по умолчанию)      Найти Antigravity CLI и применить файловый патч (patch-files)
  --restore, restore  Восстановить оригинальный бинарник CLI из бэкапа (restore-files)
  --status, status    Показать текущий статус патча CLI и системы
  --rebuild           Принудительно пересобрать Rust-движок перед запуском
  -h, --help          Показать эту справку
EOF
}

action="patch-files"
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
    --rebuild)
      rebuild=1
      shift
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

script_path=$(realpath -- "${BASH_SOURCE[0]}")
repo_dir=$(dirname -- "$(dirname -- "$script_path")")
engine="$repo_dir/target/release/antigravity-bypass-russia"

resolve_cli_path() {
  local candidate="${1:-}"
  local resolved=""
  if [[ -n $candidate ]]; then
    if [[ ! -f $candidate ]]; then
      printf 'Файл не найден: %s\n' "$candidate" >&2
      exit 1
    fi
    resolved=$(realpath -- "$candidate")
    printf '%s\n' "$resolved"
    return 0
  fi

  for name in antigravity agy; do
    candidate=$(command -v "$name" 2>/dev/null || true)
    if [[ -n $candidate && -f $candidate ]]; then
      resolved=$(realpath -- "$candidate")
      if [[ $(basename -- "$resolved") == "mise" ]]; then
        candidate=$(mise which "$name" 2>/dev/null || true)
        if [[ -n $candidate && -f $candidate ]]; then
          resolved=$(realpath -- "$candidate")
        else
          resolved=""
        fi
      fi
      if [[ -n $resolved && -f $resolved ]]; then
        printf '%s\n' "$resolved"
        return 0
      fi
    fi
  done

  local user_home="${HOME:-}"
  if [[ $(id -u) == 0 && -n ${SUDO_USER:-} ]]; then
    user_home=$(getent passwd "$SUDO_USER" | cut -d: -f6 || printf '%s' "$user_home")
  fi

  local fallback_candidates=(
    "$user_home/.local/share/mise/installs/antigravity-cli/latest/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/latest/bin/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/global/antigravity"
    "$user_home/.local/share/mise/installs/antigravity-cli/global/bin/antigravity"
    "$user_home/.local/bin/antigravity"
    "$user_home/.local/bin/agy"
    "$user_home/.gemini/antigravity-cli/bin/antigravity"
    "$user_home/.gemini/bin/antigravity"
    "$user_home/.agy/bin/agy"
  )
  for item in "${fallback_candidates[@]}"; do
    if [[ -f $item ]]; then
      resolved=$(realpath -- "$item")
      if [[ $(basename -- "$resolved") != "mise" ]]; then
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
  if [[ -n $(find "$repo_dir/src" "$repo_dir/Cargo.toml" "$repo_dir/Cargo.lock" "$repo_dir/build.rs" -newer "$engine" -print -quit 2>/dev/null) ]]; then
    return 0
  fi
  return 1
}

if needs_build; then
  if [[ $(id -u) == 0 && ${SUDO_UID:-} =~ ^[1-9][0-9]*$ ]]; then
    sudo -H -u "#$SUDO_UID" -- cargo build --release --locked --manifest-path "$repo_dir/Cargo.toml"
  else
    cargo build --release --locked --manifest-path "$repo_dir/Cargo.toml"
  fi
fi

if [[ $action == "status" ]]; then
  exec "$engine" status
fi

resolved_cli=$(resolve_cli_path "$cli_path")
exec "$engine" "$action" "$resolved_cli"
