#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
env_file="${1:-$repo_root/.env}"
backup_root="${BACKUP_ROOT:-$HOME/.local/share/contract-compliance-mvp/backups}"

if [[ ! -f "$env_file" ]]; then
  echo "Env file not found: $env_file" >&2
  exit 1
fi

mkdir -p "$backup_root"
chmod 700 "$backup_root"

timestamp="$(date +%Y%m%d-%H%M%S)"
hostname_tag="$(hostname -s 2>/dev/null || echo wsl)"
base_name="contract-compliance-mvp-env-${hostname_tag}-${timestamp}"

if command -v gpg >/dev/null 2>&1; then
  output_file="$backup_root/${base_name}.env.gpg"
  gpg --symmetric --cipher-algo AES256 --output "$output_file" "$env_file"
  method="gpg"
else
  output_file="$backup_root/${base_name}.env.enc"
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -in "$env_file" -out "$output_file"
  method="openssl"
fi

chmod 600 "$output_file"
sha256sum "$output_file" > "${output_file}.sha256"
chmod 600 "${output_file}.sha256"

echo "Encrypted backup created."
echo "Method: $method"
echo "Backup: $output_file"
echo "Checksum: ${output_file}.sha256"

if [[ "$method" == "gpg" ]]; then
  echo "Restore example: gpg --decrypt \"$output_file\" > \"$repo_root/.env\""
else
  echo "Restore example: openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in \"$output_file\" -out \"$repo_root/.env\""
fi
