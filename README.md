# Cloudbase

A self-hosted server management panel. Deploy, run and monitor apps — all from a clean web UI on your own server. Supports Docker containers, native process execution, nginx reverse proxy management, SSL certificates, multi-server node clusters, and maintenance pages.

- Web dashboard on port **7823**
- Docker-first app deployment with optional native process mode
- Process manager with auto-restart and crash recovery
- Nginx reverse proxy per app (multi-domain, redirect domains, WebSocket)
- Maintenance, update, restarting and starting pages per app
- Multi-node cluster: manage apps across multiple servers from one panel
- Systemd integration for boot autostart

---

## Requirements

- Linux (Ubuntu, Debian, RHEL, Arch, openSUSE) 
- Python 3.10+
- Nginx
- Docker (installed automatically by the installer)
- systemd (optional, for autostart)
- Git

---

## Installation

```bash
git clone https://github.com/Tonioot/Cloudbase
cd cloudbase
sudo bash install.sh
```

The installer handles everything: system packages, Python venv, nginx, Docker Engine, systemd service, nginx catch-all, and the `cloudbase` CLI.

Your administrator password is shown once at the end of the install. Open **http://your-server-ip:7823** to log in.

> **Cgroup Memory**: The installer automatically enables the cgroup memory controller in `/boot/firmware/cmdline.txt` (required for Docker memory stats). A reboot is required after install.


---

## Commands

**Core**
```
cloudbase start            Start Cloudbase
cloudbase stop             Stop Cloudbase
cloudbase restart          Restart Cloudbase
cloudbase status           Show status
cloudbase logs             View logs (systemd journal + node agent log)
cloudbase enable           Install/refresh systemd service and enable autostart
cloudbase disable          Disable systemd autostart and stop the service
cloudbase update           Pull latest changes, reinstall deps and restart
cloudbase uninstall        Completely remove Cloudbase from this system
```

**Account**
```
cloudbase password         Change the administrator password
```

**Node cluster**
```
cloudbase connect          Connect this server as a node to a main Cloudbase
    --main-url <url>       URL of the main Cloudbase instance
    --invite-code <code>   Invite code generated in the main panel
    --node-name <name>     Name for this node (default: hostname)
    --mode <mode>          panel+node (default) | node-only
cloudbase disconnect       Remove saved node connection and return to local mode
cloudbase node-status      Show current node agent connection state
```

Node mode notes:
- `--mode panel+node` (default): runs the full panel + connects as a node to the main Cloudbase.
- `--mode node-only`: runs the agent and a local API on `127.0.0.1:7823` only. The panel is not exposed publicly — useful for worker nodes that should only accept remote commands.
- Use `cloudbase logs` or `journalctl -u cloudbase` to see node command execution logs.

**Nginx**
```
cloudbase nginx <domain>   Set up nginx reverse proxy for Cloudbase itself (auto-detects SSL certs)
cloudbase nginx show       Show current nginx config
cloudbase nginx disable    Remove nginx config
cloudbase nginx permissions [user]
                           Allow Cloudbase to manage app nginx configs without sudo prompts
```

**Certificates**
```
cloudbase cert add <path> [name]   Add a certificate to the local cert store
cloudbase cert list                List stored certificates
cloudbase cert path                Show cert store location
```

**Backup & restore**
```
cloudbase export [file]    Export database + credentials to a .tar.gz archive
cloudbase import <file>    Restore database + credentials from a .tar.gz archive
```

---

## Data

All data is stored in `~/.cloudbase/`:

| Path | Contents |
|---|---|
| `cloudbase.db` | All apps, nodes and configuration |
| `credentials` | Hashed admin password |
| `secret_key` | JWT signing key |
| `certs/` | Stored SSL certificates |
| `apps/` | Cloned app repositories |
| `logs/` | Server, CLI and node agent logs |

---

## Apps

### Docker mode (default)

Apps are deployed as Docker containers by default. The installer sets up Docker Engine and adds the service user to the `docker` group.

- Each app gets an automatically assigned host port in the range **8000–8999** (`external_port`), mapped to the app's internal port.
- The Docker image is built from the app's repository using a detected or custom Dockerfile.
- Resource limits (CPU, memory) and security options (read-only root FS, tmpfs) are configurable per app.

**App actions (Docker)**
- **Start / Stop / Restart**: manage the container lifecycle.
- **Rebuild Image**: rebuild from the current code on disk (no git pull), then restart if running.
- **Pull + Rebuild + Restart**: fetch the latest commit from git, rebuild image, and restart if running.

> If a Docker app is marked as running but not reachable through nginx, check the app's internal bind address. Inside a container, web servers should bind to `0.0.0.0`, not `127.0.0.1`.

### Native process mode

Apps can also run as native OS processes (no Docker). Process isolation uses `systemd-run --user --scope` when available (survives Cloudbase restarts), falling back to a new session group.

### App types detected automatically

`nodejs`, `python`, `ruby`, `go`, `php`, `java`, `.NET` — detected from repository files (`package.json`, `requirements.txt`, `Gemfile`, `go.mod`, `composer.json`, etc.).

### Restart policies

| Policy | Behaviour |
|---|---|
| `no` | Never restart automatically |
| `always` | Restart on any exit |
| `on-failure` | Restart only on non-zero exit, with exponential backoff |

Maximum 5 restarts per 60-second window. If exceeded the app is marked as `error`.

---

## Nginx per app

Each app can have:
- A **primary domain** with optional SSL (HTTP→HTTPS redirect generated automatically).
- **Extra domains** (aliases pointing to the same app).
- **Redirect domains** (301 redirects to the primary domain).
- WebSocket upgrade support and `X-Forwarded-*` headers included in all configs.
- A custom nginx config editor in the UI.

A **default catch-all** server block is installed during setup to reject requests for unknown hostnames and prevent leaking traffic to random app configs.

---

## Maintenance & status pages

Each app can display a custom HTML page (with configurable title, message, color or full custom HTML) in four states:

| Mode | When shown |
|---|---|
| Downtime | App is manually put in downtime mode or is not reachable |
| Update | App is manually put in update mode |
| Restarting | While the app is restarting |
| Starting | While the app is starting up |

---

## Multi-node clusters

Cloudbase supports connecting multiple servers into a cluster managed from one main panel.

1. On the **main** instance: go to **Nodes** → generate an invite code.
2. On the **node** server: run `cloudbase connect --main-url <url> --invite-code <code>`.
3. The node agent connects to the main via WebSocket and relays commands and live stats.
4. Apps can be assigned to any node in the cluster and managed from the main panel.

Node stats (CPU, memory, disk) are collected and displayed per node in real time.

---

## Users & access control

Cloudbase supports multiple user accounts with two roles:

- **admin** — full access to all panel features.
- **viewer** — read-only access: can view apps, logs and stats but cannot make changes.

The built-in `admin` account created during installation is the superadmin. Only this account can manage other users via the **Manage Users** button in the sidebar.

---

## Updating

```bash
cloudbase update
```

Or manually:
```bash
cd /path/to/cloudbase
git pull
cloudbase restart
```

---

## Backup

```bash
cloudbase export ~/backup.tar.gz
```

Restore on another server:
```bash
cloudbase import ~/backup.tar.gz
cloudbase restart
```
