#!/usr/bin/env bash
set -euo pipefail

BOLD='\033[1m'
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
RESET='\033[0m'

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$INSTALL_DIR/backend"
VENV_PATH="$BACKEND_DIR/venv"
PORT=7823
CREDS="$HOME/.cloudbase/credentials"
APP_NAME="Cloudbase"
CLI_NAME="cloudbase"
SERVICE_NAME="cloudbase"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SITE_NAME="cloudbase"
NGINX_CONFIG_PATH="/etc/nginx/sites-available/${NGINX_SITE_NAME}"
NGINX_ENABLED_PATH="/etc/nginx/sites-enabled/${NGINX_SITE_NAME}"
CERTS_DIR="$HOME/.cloudbase/certs"
LOG_DIR="$HOME/.cloudbase/logs"
CLI_LOG_FILE="$LOG_DIR/cloudbase-cli.log"
AGENT_LOG_FILE="$LOG_DIR/node-agent.log"
COMMAND="${1:-start}"

mkdir -p "$CERTS_DIR" "$LOG_DIR"

if [[ $# -gt 0 ]]; then
    shift
fi

timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log_line() {
    local level="$1"
    local color="$2"
    local message="$3"
    printf '[%s] [%s] %s\n' "$(timestamp)" "$level" "$message" >> "$CLI_LOG_FILE"
    printf '%b[%s] [%s]%b %s\n' "$color" "$(timestamp)" "$level" "$RESET" "$message"
}

info()    { log_line "INFO" "$BLUE" "$1"; }
success() { log_line "OK"   "$GREEN" "$1"; }
warn()    { log_line "WARN" "$YELLOW" "$1"; }
err()     { log_line "ERR"  "$RED" "$1"; }

banner() {
    cat <<'EOF'
_________ .__                   .______.                         
\_   ___ \|  |   ____  __ __  __| _/\_ |__ _____    ______ ____  
/    \  \/|  |  /  _ \|  |  \/ __ |  | __ \\__  \  /  ___// __ \ 
\     \___|  |_(  <_> )  |  / /_/ |  | \_\ \/ __ \_\___ \\  ___/ 
 \______  /____/\____/|____/\____ |  |___  (____  /____  >\___  >
        \/                       \/      \/     \/     \/     \/ 
EOF
}

usage() {
    cat <<'EOF'
Usage: cloudbase <command> [options]

Core commands:
  start            Start Cloudbase
  stop             Stop Cloudbase
  restart          Restart Cloudbase
  status           Show Cloudbase status
  logs             Show Cloudbase service logs
  enable           Install or refresh systemd autostart and enable it now
  disable          Disable systemd autostart and stop the service
  update           Pull latest changes, reinstall deps and restart
  uninstall        Completely remove Cloudbase from this system

Account commands:
  password         Change the administrator password

Node commands:
  connect          Connect this installation as a node to a main Cloudbase
                                     Options: --main-url <url> --invite-code <code> [--node-name <name>] [--mode <panel+node|node-only>]
  disconnect       Remove saved node connection state and return to local-only mode
  node-status      Show node agent connection state

Data commands:
  export [file]    Export database + credentials to a .tar.gz archive
  import <file>    Restore database + credentials from a .tar.gz archive

Nginx commands:
  nginx <domain>   Set up nginx reverse proxy for the given domain
  nginx show       Show current nginx config
  nginx disable    Remove nginx config
  nginx permissions [user]
                   Allow Cloudbase to manage app nginx configs without password prompts

Certificate commands:
  cert add <source_path> [target_name]
  cert list
  cert path
EOF
}

service_installed() {
    [[ -f "$SERVICE_FILE" ]]
}

require_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        err "systemctl is not available on this machine"
        exit 1
    fi
}

require_nginx() {
    if ! command -v nginx >/dev/null 2>&1; then
        err "nginx is not installed"
        exit 1
    fi
}

service_run_user() {
    local run_user=""
    if service_installed && command -v systemctl >/dev/null 2>&1; then
        run_user="$(systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || true)"
    fi
    if [[ -n "$run_user" ]]; then
        printf '%s\n' "$run_user"
        return 0
    fi
    printf '%s\n' "${SUDO_USER:-$(id -un)}"
}

detect_pkg_mgr() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "apt"
    elif command -v dnf >/dev/null 2>&1; then
        echo "dnf"
    elif command -v yum >/dev/null 2>&1; then
        echo "yum"
    elif command -v pacman >/dev/null 2>&1; then
        echo "pacman"
    elif command -v zypper >/dev/null 2>&1; then
        echo "zypper"
    else
        echo ""
    fi
}

install_missing_runtime_deps() {
    local pkg_mgr="$1"
    case "$pkg_mgr" in
        apt)
            sudo apt-get update
            sudo apt-get install -y lsof python3-venv python3-pip
            ;;
        dnf)
            sudo dnf install -y lsof python3 python3-pip
            ;;
        yum)
            sudo yum install -y lsof python3 python3-pip
            ;;
        pacman)
            sudo pacman -S --noconfirm lsof python python-pip
            ;;
        zypper)
            sudo zypper install -y lsof python3 python3-pip
            ;;
        *)
            err "Could not detect a supported package manager. Install python3, python3-venv, python3-pip and lsof manually."
            exit 1
            ;;
    esac
}

port_owner_pid() {
    local pid=""
    if command -v lsof >/dev/null 2>&1; then
        pid=$(lsof -ti tcp:"$PORT" 2>/dev/null | head -n 1 || true)
    elif command -v ss >/dev/null 2>&1; then
        pid=$(ss -lptn "sport = :$PORT" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -n 1)
    fi
    echo "$pid"
}

resolve_cert_path() {
    local input="${1:-}"
    if [[ -z "$input" ]]; then
        echo ""
        return 0
    fi
    if [[ -f "$input" ]]; then
        echo "$input"
        return 0
    fi
    if [[ -f "$CERTS_DIR/$input" ]]; then
        echo "$CERTS_DIR/$input"
        return 0
    fi
    return 1
}

write_service_file() {
    require_systemctl
    local run_user="${SUDO_USER:-$(id -un)}"
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=${APP_NAME}
After=network.target

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/bin/bash ${INSTALL_DIR}/start.sh run
Restart=on-failure
RestartSec=5
KillMode=process
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
}

show_status() {
    if service_installed && command -v systemctl >/dev/null 2>&1; then
        systemctl status "$SERVICE_NAME" --no-pager
        return
    fi

    local pid
    pid="$(port_owner_pid)"
    if [[ -n "$pid" ]]; then
        success "Cloudbase is running on port $PORT (pid $pid)"
    else
        warn "Cloudbase is not running"
    fi
}

stop_foreground_instance() {
    local old_pid
    old_pid="$(port_owner_pid)"
    if [[ -n "$old_pid" ]]; then
        info "Stopping process on port $PORT (pid $old_pid)"
        kill -9 "$old_pid" 2>/dev/null || sudo kill -9 "$old_pid" || true
        success "Cloudbase API process stopped"
    else
        warn "No local Cloudbase process is listening on port $PORT"
    fi

    # Also kill any lingering node_agent.py processes (started alongside the API in node mode)
    local agent_pids
    agent_pids="$(pgrep -f "node_agent\.py" 2>/dev/null || true)"
    if [[ -n "$agent_pids" ]]; then
        info "Stopping node agent process(es): $agent_pids"
        echo "$agent_pids" | xargs kill -9 2>/dev/null || true
        success "Node agent stopped"
    fi
}

show_first_run_password() {
    local pw_file="$HOME/.cloudbase/.first-run-password"
    [[ -f "$pw_file" ]] || return 0
    local pass
    pass=$(cat "$pw_file")
    rm -f "$pw_file"
    printf '\n'
    printf '%b%s%b\n' "$YELLOW" "================================================================" "$RESET"
    printf '%b  FIRST RUN — Administrator password%b\n' "$BOLD" "$RESET"
    printf '%b  Username : admin%b\n' "$BOLD" "$RESET"
    printf '%b  Password : %b%s%b\n' "$BOLD" "$GREEN" "$pass" "$RESET"
    printf '%b  Login at : http://localhost:%s%b\n' "$BOLD" "$PORT" "$RESET"
    printf '%b  Change it via Settings in the UI after login.%b\n' "$BOLD" "$RESET"
    printf '%b%s%b\n' "$YELLOW" "================================================================" "$RESET"
    printf '\n'
}

show_logs() {
    if service_installed && command -v systemctl >/dev/null 2>&1; then
        sudo journalctl -u "$SERVICE_NAME" -n 100 --no-pager
        if [[ -f "$AGENT_LOG_FILE" ]]; then
            printf '\n'
            info "Recent node agent log lines"
            tail -n 60 "$AGENT_LOG_FILE"
        fi
        return
    fi
    if [[ -f "$CLI_LOG_FILE" ]]; then
        tail -n 100 "$CLI_LOG_FILE"
        if [[ -f "$AGENT_LOG_FILE" ]]; then
            printf '\n'
            info "Recent node agent log lines"
            tail -n 60 "$AGENT_LOG_FILE"
        fi
        return
    fi
    warn "No Cloudbase logs found yet"
}

generate_cloudbase_nginx_config() {
    local domain="$1"
    local ssl_cert="${2:-}"
    local ssl_key="${3:-}"

    if [[ -n "$ssl_cert" || -n "$ssl_key" ]]; then
        cat <<EOF
server {
    listen 80;
    server_name ${domain};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${domain};

    ssl_certificate "${ssl_cert}";
    ssl_certificate_key "${ssl_key}";
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
    }
}
EOF
        return
    fi

    cat <<EOF
server {
    listen 80;
    server_name ${domain};

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
    }
}
EOF
}

nginx_setup() {
    require_nginx
    local domain="${1:-}"
    local cert_path=""
    local key_path=""

    if [[ -z "$domain" ]]; then
        err "Usage: cloudbase nginx <domain>"
        exit 1
    fi

    # Auto-detect certificates from the certs directory
    local auto_cert="$CERTS_DIR/fullchain.pem"
    local auto_key="$CERTS_DIR/privkey.pem"
    if [[ -f "$auto_cert" && -f "$auto_key" ]]; then
        cert_path="$auto_cert"
        key_path="$auto_key"
        info "Found SSL certificates in $CERTS_DIR — enabling HTTPS"
    else
        info "No SSL certificates found in $CERTS_DIR — setting up HTTP proxy"
        info "  To enable HTTPS: cloudbase cert add <fullchain.pem> then cloudbase cert add <privkey.pem>"
    fi

    info "Writing nginx config for Cloudbase domain '$domain'"
    generate_cloudbase_nginx_config "$domain" "$cert_path" "$key_path" | sudo tee "$NGINX_CONFIG_PATH" > /dev/null
    sudo ln -sf "$NGINX_CONFIG_PATH" "$NGINX_ENABLED_PATH"
    sudo nginx -t
    sudo systemctl reload nginx

    if [[ -n "$cert_path" ]]; then
        success "Cloudbase is now available at https://$domain"
    else
        success "Cloudbase is now available at http://$domain"
    fi
}

nginx_show() {
    require_nginx
    if sudo test -f "$NGINX_CONFIG_PATH"; then
        sudo cat "$NGINX_CONFIG_PATH"
    else
        warn "No local Cloudbase nginx config exists yet"
    fi
}

nginx_disable() {
    require_nginx
    info "Removing local Cloudbase nginx config"
    sudo rm -f "$NGINX_ENABLED_PATH" "$NGINX_CONFIG_PATH"
    sudo nginx -t
    sudo systemctl reload nginx
    success "Cloudbase nginx config removed"
}

nginx_permissions() {
    require_nginx
    local target_user="${1:-$(service_run_user)}"
    info "Configuring Cloudbase nginx permissions for user '$target_user'"
    bash "$INSTALL_DIR/scripts/setup-nginx-permissions.sh" "$target_user"
    success "Cloudbase nginx permissions configured"
}

handle_nginx_command() {
    local subcommand="${1:-}"
    shift || true
    case "$subcommand" in
        permissions|perms|fix-perms)
            nginx_permissions "$@"
            ;;
        show)
            nginx_show
            ;;
        disable|remove)
            nginx_disable
            ;;
        "")
            err "Usage: cloudbase nginx <domain>"
            exit 1
            ;;
        *)
            # Treat anything else as a domain name
            nginx_setup "$subcommand" "$@"
            ;;
    esac
}

cert_add() {
    local source_path="${1:-}"
    local target_name="${2:-}"
    if [[ -z "$source_path" ]]; then
        err "Usage: cloudbase cert add <source_path> [target_name]"
        exit 1
    fi
    if [[ ! -f "$source_path" ]]; then
        err "Certificate file not found: $source_path"
        exit 1
    fi
    if [[ -z "$target_name" ]]; then
        target_name="$(basename "$source_path")"
    fi
    cp "$source_path" "$CERTS_DIR/$target_name"
    success "Copied certificate to $CERTS_DIR/$target_name"
}

cert_list() {
    if compgen -G "$CERTS_DIR/*" > /dev/null; then
        ls -1 "$CERTS_DIR"
    else
        warn "No local certificates found in $CERTS_DIR"
    fi
}

handle_cert_command() {
    local subcommand="${1:-}"
    shift || true
    case "$subcommand" in
        add)
            cert_add "$@"
            ;;
        list)
            cert_list
            ;;
        path)
            printf '%s\n' "$CERTS_DIR"
            ;;
        *)
            err "Usage: cloudbase cert <add|list|path>"
            exit 1
            ;;
    esac
}

cmd_password() {
    info "Changing password for the admin account"
    local new_pass
    read -r -s -p "New password: " new_pass
    printf '\n'
    if [[ ${#new_pass} -lt 8 ]]; then
        err "Password must be at least 8 characters"
        exit 1
    fi
    local confirm
    read -r -s -p "Confirm password: " confirm
    printf '\n'
    if [[ "$new_pass" != "$confirm" ]]; then
        err "Passwords do not match"
        exit 1
    fi
    cd "$BACKEND_DIR"
    "$VENV_PATH/bin/python3" - <<PYEOF
import sys
sys.path.insert(0, '.')
import auth
auth.save_hashed_password(auth.hash_password('$new_pass'))
print('Password updated')
PYEOF
    success "Admin password updated"
}

cmd_export() {
    local out="${1:-cloudbase-backup-$(date +%Y%m%d-%H%M%S).tar.gz}"
    local data_dir="$HOME/.cloudbase"
    local tmp_dir
    tmp_dir=$(mktemp -d)
    mkdir -p "$tmp_dir/cloudbase"
    [[ -f "$data_dir/cloudbase.db" ]]  && cp "$data_dir/cloudbase.db"  "$tmp_dir/cloudbase/"
    [[ -f "$data_dir/credentials" ]]   && cp "$data_dir/credentials"   "$tmp_dir/cloudbase/"
    [[ -f "$data_dir/secret_key" ]]    && cp "$data_dir/secret_key"    "$tmp_dir/cloudbase/"
    if [[ -d "$data_dir/certs" ]]; then
        cp -r "$data_dir/certs" "$tmp_dir/cloudbase/"
    fi
    tar -czf "$out" -C "$tmp_dir" cloudbase
    rm -rf "$tmp_dir"
    success "Export saved to $out"
}

cmd_import() {
    local src="${1:-}"
    if [[ -z "$src" || ! -f "$src" ]]; then
        err "Usage: cloudbase import <backup.tar.gz>"
        exit 1
    fi
    local data_dir="$HOME/.cloudbase"
    local tmp_dir
    tmp_dir=$(mktemp -d)
    tar -xzf "$src" -C "$tmp_dir"
    local src_dir="$tmp_dir/cloudbase"
    if [[ ! -d "$src_dir" ]]; then
        err "Invalid backup archive: missing cloudbase/ directory"
        rm -rf "$tmp_dir"
        exit 1
    fi
    mkdir -p "$data_dir"
    [[ -f "$src_dir/cloudbase.db" ]] && cp "$src_dir/cloudbase.db" "$data_dir/" && success "Database restored"
    [[ -f "$src_dir/credentials" ]]  && cp "$src_dir/credentials"  "$data_dir/" && chmod 600 "$data_dir/credentials"
    [[ -f "$src_dir/secret_key" ]]   && cp "$src_dir/secret_key"   "$data_dir/" && chmod 600 "$data_dir/secret_key"
    if [[ -d "$src_dir/certs" ]]; then
        cp -r "$src_dir/certs" "$data_dir/"
        success "Certificates restored"
    fi
    rm -rf "$tmp_dir"
    success "Import complete — restart Cloudbase to apply: cloudbase restart"
}

cmd_update() {
    if ! command -v git >/dev/null 2>&1; then
        err "git is not installed"
        exit 1
    fi
    cd "$INSTALL_DIR"
    info "Fetching latest changes"
    git fetch origin
    info "Hard resetting to origin/main"
    git reset --hard origin/main
    git clean -fd --exclude="backend/agent_state.json" --exclude="backend/node_agent.log" --exclude="backend/venv/" --exclude="*.db"
    info "Reinstalling Python dependencies"
    "$VENV_PATH/bin/pip" install --quiet --upgrade pip
    "$VENV_PATH/bin/pip" install --quiet -r "$BACKEND_DIR/requirements.txt"
    success "Dependencies updated"
    info "Restarting Cloudbase"
    if service_installed && command -v systemctl >/dev/null 2>&1; then
        info "Refreshing systemd service file"
        write_service_file
        sudo systemctl daemon-reload
        sudo systemctl restart "$SERVICE_NAME"
        success "Cloudbase updated and restarted"
    else
        stop_foreground_instance
        run_runtime
    fi
}


cmd_disconnect() {
    local state_file="$BACKEND_DIR/agent_state.json"
    local mode_file="$HOME/.cloudbase/mode"

    if [[ -f "$state_file" ]]; then
        rm -f "$state_file"
        success "Removed saved node connection state"
    else
        warn "No saved node connection state found"
    fi

    if [[ -f "$mode_file" ]]; then
        rm -f "$mode_file"
        info "Cleared saved node mode"
    fi

    if service_installed && command -v systemctl >/dev/null 2>&1; then
        info "Restarting Cloudbase service to apply disconnect"
        sudo systemctl restart "$SERVICE_NAME"
        success "Disconnected from main node and restarted locally"
    else
        success "Disconnected from main node"
        info "Run 'cloudbase restart' to apply mode changes in this session"
    fi
}


run_runtime() {
    info "Starting Cloudbase runtime"

    if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1 || ! command -v lsof >/dev/null 2>&1; then
        warn "Missing runtime dependencies detected. Installing them now."
        install_missing_runtime_deps "$(detect_pkg_mgr)"
    fi

    mkdir -p "$(dirname "$CREDS")"

    local old_pid
    old_pid="$(port_owner_pid)"
    if [[ -n "$old_pid" ]]; then
        warn "Cleaning up old process on port $PORT (pid $old_pid)"
        kill -9 "$old_pid" 2>/dev/null || sudo kill -9 "$old_pid" || true
    fi

    cd "$BACKEND_DIR"

    if [[ ! -d "$VENV_PATH" || ! -f "$VENV_PATH/bin/pip" ]]; then
        warn "Virtual environment missing or broken. Rebuilding it."
        rm -rf "$VENV_PATH"
        python3 -m venv venv
        success "Virtual environment rebuilt"
    fi

    info "Syncing Python dependencies"
    "$VENV_PATH/bin/pip" install --upgrade pip
    "$VENV_PATH/bin/pip" install -r requirements.txt

    if [[ ! -f "$CREDS" ]]; then
        info "First run detected. Generating administrator password"
        local pass
        pass=$("$VENV_PATH/bin/python3" -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits + '!@#%^&*') for _ in range(20)))")

        "$VENV_PATH/bin/python3" - <<PYEOF
import sys
sys.path.insert(0, '.')
import auth
auth.save_hashed_password(auth.hash_password("$pass"))
PYEOF

        printf '%s' "$pass" > "$HOME/.cloudbase/.first-run-password"
    fi

    success "Launching Cloudbase on port $PORT"
    exec "$VENV_PATH/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 8
}


run_node_only_runtime() {
    info "Running in node-only mode (agent integrated in API on 127.0.0.1)"

    if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1 || ! command -v lsof >/dev/null 2>&1; then
        warn "Missing runtime dependencies detected. Installing them now."
        install_missing_runtime_deps "$(detect_pkg_mgr)"
    fi

    mkdir -p "$(dirname "$CREDS")"
    cd "$BACKEND_DIR"

    if [[ ! -d "$VENV_PATH" || ! -f "$VENV_PATH/bin/pip" ]]; then
        warn "Virtual environment missing or broken. Rebuilding it."
        rm -rf "$VENV_PATH"
        python3 -m venv venv
        success "Virtual environment rebuilt"
    fi

    info "Syncing Python dependencies"
    "$VENV_PATH/bin/pip" install --upgrade pip
    "$VENV_PATH/bin/pip" install -r requirements.txt

    local old_pid
    old_pid="$(port_owner_pid)"
    if [[ -n "$old_pid" ]]; then
        warn "Cleaning up old process on port $PORT (pid $old_pid)"
        kill -9 "$old_pid" 2>/dev/null || sudo kill -9 "$old_pid" || true
    fi

    success "Launching Cloudbase node on port $PORT"
    exec "$VENV_PATH/bin/uvicorn" main:app --host 127.0.0.1 --port "$PORT" --timeout-graceful-shutdown 8
}

printf '\n%b' "$BOLD"
banner
printf '%b\n' "$RESET"
info "Cloudbase CLI log: $CLI_LOG_FILE"

case "$COMMAND" in
    help|-h|--help)
        usage
        exit 0
        ;;
    enable)
        write_service_file
        sudo systemctl daemon-reload
        sudo systemctl enable --now "$SERVICE_NAME"
        success "Cloudbase now starts automatically on boot"
        ;;
    disable)
        require_systemctl
        if service_installed; then
            sudo systemctl disable --now "$SERVICE_NAME"
            success "Cloudbase boot autostart disabled"
        else
            warn "No Cloudbase systemd service is installed"
        fi
        ;;
    status)
        show_status
        ;;
    stop)
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            success "Cloudbase service stopped"
        fi
        stop_foreground_instance
        ;;
    restart)
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl restart "$SERVICE_NAME"
            success "Cloudbase service restarted"
        else
            stop_foreground_instance
            run_runtime
        fi
        ;;
    logs)
        show_logs
        ;;
    password)
        cmd_password
        ;;
    export)
        cmd_export "$@"
        ;;
    import)
        cmd_import "$@"
        ;;
    update)
        cmd_update
        ;;
    nginx)
        handle_nginx_command "$@"
        ;;
    cert|certs)
        handle_cert_command "$@"
        ;;
    connect)
        # Parse --main-url, --invite-code, --node-name, --mode flags
        _MAIN_URL=""
        _INVITE_CODE=""
        _NODE_NAME="$(hostname)"
        _MODE="panel+node"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --main-url)    _MAIN_URL="$2";    shift 2 ;;
                --invite-code) _INVITE_CODE="$2"; shift 2 ;;
                --node-name)   _NODE_NAME="$2";   shift 2 ;;
                --mode)        _MODE="$2";        shift 2 ;;
                *) shift ;;
            esac
        done
        if [[ -z "$_MAIN_URL" || -z "$_INVITE_CODE" ]]; then
            # Check if already registered (saved state exists)
            if [[ ! -f "$HOME/.cloudbase-node/agent_state.json" ]]; then
                err "Usage: cloudbase connect --main-url <url> --invite-code <code> [--node-name <name>] [--mode <panel+node|node-only>]"
                exit 1
            fi
            # Load previous mode if available
            if [[ -f "$HOME/.cloudbase/mode" ]]; then
                _MODE=$(cat "$HOME/.cloudbase/mode")
            fi
        else
            # New connection params provided — auto-disconnect if already connected
            if [[ -f "$BACKEND_DIR/agent_state.json" ]]; then
                info "Already connected — disconnecting first before reconnecting..."
                cmd_disconnect
            fi
        fi
        cd "$BACKEND_DIR"
        if [[ ! -d "$VENV_PATH" ]]; then
            err "Cloudbase not installed. Run install.sh first, then cloudbase connect."
            exit 1
        fi
        # Save mode for future starts
        mkdir -p "$HOME/.cloudbase"
        echo "$_MODE" > "$HOME/.cloudbase/mode"
        info "Connecting to main Cloudbase${_MAIN_URL:+ at $_MAIN_URL} (mode: $_MODE)"
        CONNECT_ARGS=()
        [[ -n "$_MAIN_URL" ]]    && CONNECT_ARGS+=(--main-url    "$_MAIN_URL")
        [[ -n "$_INVITE_CODE" ]] && CONNECT_ARGS+=(--invite-code "$_INVITE_CODE")
        [[ -n "$_NODE_NAME" ]]   && CONNECT_ARGS+=(--node-name   "$_NODE_NAME")

        if [[ "$_MODE" == "node-only" ]]; then
            # Only register if no saved state exists yet
            if [[ -n "$_MAIN_URL" && ! -f "$BACKEND_DIR/agent_state.json" ]]; then
                info "Performing initial node registration..."
                "$VENV_PATH/bin/python" node_agent.py "${CONNECT_ARGS[@]}" --exit-after-registration
            elif [[ -f "$BACKEND_DIR/agent_state.json" ]]; then
                info "Already registered — skipping re-registration"
            fi

            # Start/Restart service or run in foreground
            if service_installed && command -v systemctl >/dev/null 2>&1; then
                sudo systemctl restart "$SERVICE_NAME"
                success "Cloudbase node connected and service restarted"
            else
                run_node_only_runtime
            fi
            exit 0
        else
            # Panel + Node mode: run panel + agent integrated
            if [[ -n "$_MAIN_URL" && ! -f "$BACKEND_DIR/agent_state.json" ]]; then
                info "Performing initial node registration..."
                "$VENV_PATH/bin/python" node_agent.py "${CONNECT_ARGS[@]}" --exit-after-registration
            elif [[ -f "$BACKEND_DIR/agent_state.json" ]]; then
                info "Already registered — skipping re-registration"
            fi

            if service_installed && command -v systemctl >/dev/null 2>&1; then
                sudo systemctl restart "$SERVICE_NAME"
                success "Cloudbase service restarted with node integration"
            else
                exec "$VENV_PATH/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT" --timeout-graceful-shutdown 8
            fi
        fi
        ;;
    disconnect)
        cmd_disconnect
        ;;
    node-status)
        state_file="$BACKEND_DIR/agent_state.json"
        if [[ ! -f "$state_file" ]]; then
            warn "Not connected to any main node (no agent_state.json)"
        else
            node_id=$(python3 -c "import json; d=json.load(open('$state_file')); print(d.get('node_id','?'))" 2>/dev/null)
            node_name=$(python3 -c "import json; d=json.load(open('$state_file')); print(d.get('node_name','?'))" 2>/dev/null)
            main_url=$(python3 -c "import json; d=json.load(open('$state_file')); print(d.get('main_url','?'))" 2>/dev/null)
            info "Registered as: $node_name (id=$node_id)"
            info "Main node:     $main_url"
            # Check if agent process is running
            if pgrep -f "node_agent.py" >/dev/null 2>&1; then
                success "Agent process: running"
            else
                warn "Agent process: not running"
            fi
        fi
        ;;
    start)
        show_first_run_password
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            sudo systemctl start "$SERVICE_NAME"
            success "Cloudbase service started"
        else
            # Check if running as a node
            if [[ -f "$HOME/.cloudbase/mode" && "$(cat $HOME/.cloudbase/mode)" == "node-only" ]]; then
                run_node_only_runtime
            else
                run_runtime
            fi
        fi
        ;;
    run)
        # Check if running as a node
        if [[ -f "$HOME/.cloudbase/mode" && "$(cat $HOME/.cloudbase/mode)" == "node-only" ]]; then
            run_node_only_runtime
        else
            run_runtime
        fi
        ;;
    uninstall)
        printf '%b\n' "${RED}WARNING: This will permanently remove Cloudbase from this system.${RESET}"
        printf '%b\n' "${RED}The following will be deleted:${RESET}"
        printf '  - Systemd service (%s)\n' "$SERVICE_FILE"
        printf '  - Cloudbase install directory (%s)\n' "$INSTALL_DIR"
        printf '  - Cloudbase data directory (%s)\n' "$HOME/.cloudbase"
        printf '  - Nginx config (if present)\n'
        printf '  - cloudbase symlink in /usr/local/bin (if present)\n'
        printf '\n'
        read -r -p "Type 'yes' to confirm uninstall: " confirm
        if [[ "$confirm" != "yes" ]]; then
            warn "Uninstall cancelled"
            exit 0
        fi

        # Stop and remove systemd service
        if service_installed && command -v systemctl >/dev/null 2>&1; then
            info "Stopping and disabling Cloudbase service"
            sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
            sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
            sudo rm -f "$SERVICE_FILE"
            sudo systemctl daemon-reload
        else
            stop_foreground_instance 2>/dev/null || true
        fi

        # Remove nginx config
        if command -v nginx >/dev/null 2>&1; then
            if sudo test -f "$NGINX_CONFIG_PATH" || sudo test -f "$NGINX_ENABLED_PATH"; then
                info "Removing nginx config"
                sudo rm -f "$NGINX_ENABLED_PATH" "$NGINX_CONFIG_PATH"
                sudo nginx -t 2>/dev/null && sudo systemctl reload nginx 2>/dev/null || true
            fi
        fi

        # Remove cloudbase symlink
        if [[ -L "/usr/local/bin/$CLI_NAME" ]]; then
            info "Removing /usr/local/bin/$CLI_NAME symlink"
            sudo rm -f "/usr/local/bin/$CLI_NAME"
        fi

        # Remove install directory first (this script is inside it — use sudo for root-owned venv files)
        printf '[%s] [INFO] Removing install directory %s\n' "$(timestamp)" "$INSTALL_DIR"
        sudo rm -rf "$INSTALL_DIR"

        # Remove data directory last — log file is inside it so no more logging after this
        printf '[%s] [INFO] Removing data directory %s\n' "$(timestamp)" "$HOME/.cloudbase"
        sudo rm -rf "$HOME/.cloudbase"

        printf '[%s] [OK] Cloudbase has been completely removed from this system\n' "$(timestamp)"
        ;;
    *)
        err "Unknown command: $COMMAND"
        usage
        exit 1
        ;;
esac
