#!/usr/bin/env bash
set -euo pipefail

TARGET_USER="${1:-${SUDO_USER:-$(id -un)}}"
SUDOERS_FILE="/etc/sudoers.d/cloudbase-nginx"

find_cmd() {
  local name="$1"
  local path
  path="$(command -v "$name" || true)"
  if [[ -z "$path" ]]; then
    echo "Required command not found: $name" >&2
    exit 1
  fi
  printf '%s\n' "$path"
}

MKTEMP_BIN="$(find_cmd mktemp)"
VISUDO_BIN="$(find_cmd visudo)"
MKDIR_BIN="$(find_cmd mkdir)"
TEE_BIN="$(find_cmd tee)"
CHMOD_BIN="$(find_cmd chmod)"
LN_BIN="$(find_cmd ln)"
NGINX_BIN="$(find_cmd nginx)"
SYSTEMCTL_BIN="$(find_cmd systemctl)"
RM_BIN="$(find_cmd rm)"
SUDO_BIN="$(find_cmd sudo)"

TMP_FILE="$("$MKTEMP_BIN")"
cleanup() {
  rm -f "$TMP_FILE"
}
trap cleanup EXIT

cat >"$TMP_FILE" <<EOF
Defaults:${TARGET_USER} !requiretty
Cmnd_Alias CLOUDBASE_NGINX = \\
  ${MKDIR_BIN} -p /var/www/cloudbase/maintenance/*, \\
  ${TEE_BIN} /var/www/cloudbase/maintenance/*/*, \\
  ${CHMOD_BIN} 644 /var/www/cloudbase/maintenance/*/*, \\
  ${TEE_BIN} /etc/nginx/sites-available/*, \\
  ${LN_BIN} -sf /etc/nginx/sites-available/* /etc/nginx/sites-enabled/*, \\
  ${NGINX_BIN} -t, \\
  ${SYSTEMCTL_BIN} reload nginx, \\
  ${RM_BIN} -f /etc/nginx/sites-enabled/*, \\
  ${RM_BIN} -f /etc/nginx/sites-available/*
${TARGET_USER} ALL=(root) NOPASSWD: CLOUDBASE_NGINX
EOF

"$SUDO_BIN" "$VISUDO_BIN" -cf "$TMP_FILE"
"$SUDO_BIN" "$MKDIR_BIN" -p /etc/sudoers.d
"$SUDO_BIN" "$TEE_BIN" "$SUDOERS_FILE" >/dev/null <"$TMP_FILE"
"$SUDO_BIN" "$CHMOD_BIN" 440 "$SUDOERS_FILE"
"$SUDO_BIN" "$VISUDO_BIN" -cf "$SUDOERS_FILE"

"$SUDO_BIN" "$MKDIR_BIN" -p /var/www/cloudbase/maintenance
"$SUDO_BIN" "$CHMOD_BIN" 755 /var/www/cloudbase/maintenance

printf 'Configured Cloudbase nginx permissions for user: %s\n' "$TARGET_USER"
printf 'Sudoers file: %s\n' "$SUDOERS_FILE"
