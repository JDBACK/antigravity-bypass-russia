#!/usr/bin/env bash
# Compatibility wrapper for the unified cross-platform CLI patcher (Linux & macOS).
set -euo pipefail

source_path="${BASH_SOURCE[0]}"
while [[ -L $source_path ]]; do
  source_dir=$(cd -- "$(dirname -- "$source_path")" && pwd -P)
  link_target=$(readlink "$source_path")
  if [[ $link_target == /* ]]; then
    source_path="$link_target"
  else
    source_path="$source_dir/$link_target"
  fi
done
script_dir=$(cd -- "$(dirname -- "$source_path")" && pwd -P)
exec "$script_dir/patch_cli.sh" "$@"
