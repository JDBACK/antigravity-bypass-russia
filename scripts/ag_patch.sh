#!/usr/bin/env bash
# Unified file-only Antigravity patcher for MacBook + homelinux (no DNS/hosts edits).
set -euo pipefail

os_name="${AG_PATCH_OS:-$(uname -s)}"
case "$os_name" in
  Darwin|Linux) ;;
  *)
    printf 'Скрипт поддерживает только macOS и Linux (обнаружено: %s).\n' "$os_name" >&2
    exit 1
    ;;
esac

usage() {
  cat <<'EOF'
Использование:
  ag-patch [ОПЦИИ]

Режимы:
  (по умолчанию)      Пропатчить все найденные файлы Antigravity (без изменения DNS/сети).
                      На macOS патчит сначала MacBook, затем homelinux по SSH.
                      На Linux патчит локально.
  --status, status    Показать статус файлов и открытых процессов (без изменений)
  --dry-run           Показать план патча и открытые процессы без закрытия и изменений
  --restore, restore  Откатить файловые патчи из бэкапа (без изменения DNS/сети)

Выбор устройств:
  --mac, --local      Только текущее устройство (без SSH на Linux)
  --linux             Только удалённый Linux по SSH (homelinux)
  --ssh-host <host>   SSH-хост Linux-машины (по умолчанию: homelinux или AG_PATCH_SSH_HOST)

Прочее:
  --rebuild           Принудительно пересобрать Rust-движок перед запуском
  -h, --help          Показать эту справку
EOF
}

mode="patch"
dry_run=0
scope="auto"
rebuild=0
ssh_host="${AG_PATCH_SSH_HOST:-homelinux}"
ssh_bin="${AG_PATCH_SSH_BIN:-ssh}"
remote_cmd="${AG_PATCH_REMOTE_CMD:-~/antigravity-bypass-russia/scripts/ag_patch.sh}"

while (( $# > 0 )); do
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
    --status|status)
      mode="status"
      shift
      ;;
    --restore|restore)
      mode="restore"
      shift
      ;;
    --patch|patch)
      mode="patch"
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --mac|--local)
      scope="local"
      shift
      ;;
    --linux)
      scope="remote"
      shift
      ;;
    --ssh-host)
      if (( $# < 2 )) || [[ -z ${2:-} ]]; then
        printf 'Для --ssh-host нужно указать имя хоста.\n' >&2
        usage >&2
        exit 2
      fi
      ssh_host="$2"
      shift 2
      ;;
    --rebuild)
      rebuild=1
      shift
      ;;
    *)
      printf 'Неизвестный аргумент: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ $scope == "remote" && $os_name == "Linux" && -z ${AG_PATCH_ALLOW_LINUX_SSH:-} ]]; then
  scope="local"
fi

resolve_self_dir() {
  local source_path="${BASH_SOURCE[0]}"
  local source_dir=""
  local link_target=""
  while [[ -L $source_path ]]; do
    source_dir=$(cd -- "$(dirname -- "$source_path")" && pwd -P)
    link_target=$(readlink "$source_path")
    if [[ $link_target == /* ]]; then
      source_path="$link_target"
    else
      source_path="$source_dir/$link_target"
    fi
  done
  cd -- "$(dirname -- "$source_path")" && pwd -P
}

find_cargo() {
  local candidate=""
  for candidate in \
    "$(command -v cargo 2>/dev/null || true)" \
    "${CARGO_HOME:-$HOME/.cargo}/bin/cargo" \
    "$HOME/.cargo/bin/cargo" \
    "/opt/homebrew/bin/cargo" \
    "/usr/local/bin/cargo" \
    "/usr/bin/cargo"
  do
    if [[ -n $candidate && -x $candidate ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

script_dir=$(resolve_self_dir)
repo_dir=$(cd -- "$script_dir/.." && pwd -P)
engine="${AG_PATCH_ENGINE:-$repo_dir/target/release/antigravity-bypass-russia}"

ensure_engine() {
  if [[ -n ${AG_PATCH_ENGINE:-} ]]; then
    if [[ ! -x $engine ]]; then
      printf 'Движок не найден: %s\n' "$engine" >&2
      exit 1
    fi
    return 0
  fi

  local need_build=0
  if (( rebuild == 1 )) || [[ ! -x $engine ]]; then
    need_build=1
  else
    local newer=""
    newer=$(find "$repo_dir/src" "$repo_dir/Cargo.toml" "$repo_dir/Cargo.lock" "$repo_dir/build.rs" -newer "$engine" -print 2>/dev/null || true)
    if [[ -n $newer ]]; then
      need_build=1
    fi
  fi

  if (( need_build == 1 )); then
    local cargo_bin=""
    if ! cargo_bin=$(find_cargo); then
      printf 'Rust/Cargo не найден. Установите rustup (https://rustup.rs) и повторите.\n' >&2
      exit 1
    fi
    local cargo_dir=""
    cargo_dir=$(dirname -- "$cargo_bin")
    printf 'Собираю Rust-движок...\n'
    PATH="$cargo_dir:$PATH" "$cargo_bin" build --release --locked --manifest-path "$repo_dir/Cargo.toml"
  fi
}

host_label() {
  if [[ $os_name == "Darwin" ]]; then
    printf 'MacBook (macOS)'
  else
    printf '%s (Linux)' "$(hostname -s 2>/dev/null || hostname)"
  fi
}

print_banner() {
  local title="$1"
  printf '\n=== %s ===\n' "$title"
}

run_inspect_report() {
  local inspect_json="$1"
  local action_mode="$2"
  local is_dry_run="$3"
  python3 -c '
import json
import sys

mode = sys.argv[1]
dry_run = sys.argv[2] == "1"
data = json.load(sys.stdin)

targets = data.get("targets", [])
state_labels = {
    "patched": "✓ настроено",
    "stock": "! не настроено",
    "partial": "! настроено частично",
    "unknown": "? нужна проверка",
}

if not targets:
    print("  Файлы Antigravity не найдены.")
else:
    for item in targets:
        mark = state_labels.get(item.get("state", ""), item.get("state", ""))
        label = item.get("label", "?")
        path = item.get("path", "")
        print(f"  • {label}: {path} [{mark}]")

if mode == "restore":
    pending_count = int(data.get("needs_restore_count", 0))
    holders = data.get("restore_holders_summary", "") or ""
    needs_admin = bool(data.get("needs_admin_for_restore", False))
    done_msg = "✓ Все файлы уже в исходном состоянии — менять ничего не нужно."
    plan_verb = "откатить"
else:
    pending_count = int(data.get("needs_patch_count", 0))
    holders = data.get("patch_holders_summary", "") or ""
    needs_admin = bool(data.get("needs_admin_for_patch", False))
    done_msg = "✓ Все найденные файлы уже настроены — закрывать и менять ничего не нужно."
    plan_verb = "пропатчить"

holders_error = data.get("holders_error")
if holders_error:
    print(f"  ! Не удалось проверить открытые процессы: {holders_error}")

if pending_count == 0:
    print(f"  {done_msg}")
else:
    if dry_run or mode == "status":
        sudo_note = "потребуется sudo" if needs_admin else "без sudo"
        print(f"  → К обновлению ({plan_verb}): {pending_count} файл(ов), {sudo_note}.")
        if holders:
            print(f"  ! Сейчас открыто: {holders}")
            if dry_run:
                print("    При реальном запуске будет предложено закрыть эти процессы перед изменением файлов.")
        elif dry_run:
            print("  ✓ Блокирующих процессов нет — можно менять файлы сразу.")
' "$action_mode" "$is_dry_run" <<<"$inspect_json"
}

extract_pending_count() {
  local inspect_json="$1"
  local action_mode="$2"
  python3 -c '
import json
import sys

mode = sys.argv[1]
data = json.load(sys.stdin)
key = "needs_restore_count" if mode == "restore" else "needs_patch_count"
print(int(data.get(key, 0)))
' "$action_mode" <<<"$inspect_json"
}

run_local() {
  ensure_engine
  local label
  label=$(host_label)
  print_banner "$label"

  local inspect_json=""
  if ! inspect_json=$("$engine" inspect-files --json); then
    printf '✗ %s: не удалось проверить файлы Antigravity.\n' "$label" >&2
    return 1
  fi

  if [[ $mode == "status" ]]; then
    "$engine" status
    run_inspect_report "$inspect_json" "patch" "0"
    return 0
  fi

  if (( dry_run == 1 )); then
    printf '  [dry-run] Режим проверки без закрытия приложений и без изменения файлов:\n'
    run_inspect_report "$inspect_json" "$mode" "1"
    return 0
  fi

  local pending_count
  pending_count=$(extract_pending_count "$inspect_json" "$mode")
  if (( pending_count == 0 )); then
    run_inspect_report "$inspect_json" "$mode" "0"
    return 0
  fi

  run_inspect_report "$inspect_json" "$mode" "0"
  local engine_cmd="patch-files"
  if [[ $mode == "restore" ]]; then
    engine_cmd="restore-files"
  fi

  if AGY_PROMPT_CLOSE=1 "$engine" "$engine_cmd" --prompt-close; then
    return 0
  fi
  return 1
}

run_remote() {
  local remote_args=(--local)
  case "$mode" in
    status) remote_args+=(--status) ;;
    restore) remote_args+=(--restore) ;;
    patch) ;;
  esac
  if (( dry_run == 1 )); then
    remote_args+=(--dry-run)
  fi
  if (( rebuild == 1 )); then
    remote_args+=(--rebuild)
  fi

  if ! "$ssh_bin" -o BatchMode=yes -o ConnectTimeout=5 "$ssh_host" true >/dev/null 2>&1; then
    print_banner "$ssh_host (Linux)"
    printf '  ! Не удалось подключиться к %s по SSH (хост недоступен или выключен).\n' "$ssh_host" >&2
    return 1
  fi

  local ssh_tty_flag="-T"
  if [[ -t 0 && -t 1 ]]; then
    ssh_tty_flag="-t"
  fi

  local quoted_args=""
  local arg=""
  for arg in "${remote_args[@]}"; do
    quoted_args+=" $(printf '%q' "$arg")"
  done

  "$ssh_bin" "$ssh_tty_flag" "$ssh_host" "$remote_cmd$quoted_args"
}

main() {
  local local_rc=0
  local remote_rc=0
  local ran_local=0
  local ran_remote=0

  if [[ $scope == "local" ]] || [[ $scope == "auto" && $os_name == "Linux" ]]; then
    ran_local=1
    run_local || local_rc=$?
  elif [[ $scope == "remote" ]]; then
    ran_remote=1
    run_remote || remote_rc=$?
  else
    # auto on macOS: MacBook first, then Linux via SSH
    ran_local=1
    run_local || local_rc=$?

    local proceed_remote=1
    if (( local_rc != 0 && dry_run == 0 )) && [[ $mode != "status" && -t 0 && -t 1 ]]; then
      printf '\nMacBook завершился с отменой или ошибкой. Перейти к %s? [Y/n]: ' "$ssh_host"
      local answer=""
      if read -r answer; then
        case "$answer" in
          n|N|н|Н|no|NO|нет|НЕТ) proceed_remote=0 ;;
        esac
      else
        proceed_remote=0
      fi
    fi

    if (( proceed_remote == 1 )); then
      ran_remote=1
      run_remote || remote_rc=$?
    fi
  fi

  if (( ran_local == 1 && ran_remote == 1 )); then
    printf '\n=== Итог ===\n'
    if (( local_rc == 0 )); then
      printf '  ✓ MacBook: готово\n'
    else
      printf '  ✗ MacBook: не завершено (код %d)\n' "$local_rc"
    fi
    if (( remote_rc == 0 )); then
      printf '  ✓ %s: готово\n' "$ssh_host"
    else
      printf '  ✗ %s: не завершено (код %d)\n' "$ssh_host" "$remote_rc"
    fi
  fi

  if (( local_rc != 0 )); then
    exit "$local_rc"
  fi
  if (( remote_rc != 0 )); then
    exit "$remote_rc"
  fi
  exit 0
}

main
