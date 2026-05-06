# Cloudbase
Original Cloudbase project by Tonioot (2026)  
This repository is the canonical and original source of Cloudbase.

Cloudbase is a self-hosted deployment and operations panel for running applications across one or more servers.

It gives you one place to deploy from Git, scale with instances, manage networking and SSL, monitor runtime metrics, and operate a multi-node setup.

## Overview

Cloudbase runs on port 7823 by default and includes:

- App deployments from Git repositories
- Docker-first runtime with optional native process mode
- Instance-based scaling and load balancing
- Multi-node orchestration from one primary panel
- Nginx config management per app (domains, redirects, SSL)
- Zero-downtime restart flow
- Live logs, stats, and audit events
- User roles and access control

## Core model: App, Instance, Node

This is the most important concept in Cloudbase:

- An App is a logical service definition. It stores configuration like repository, start command, internal port, domains, environment variables, and runtime limits.
- An Instance is a running replica of an app. Each instance has its own runtime process/container and its own external host port.
- A Node is a server that runs instances.

Key rule:

- Apps do not run on nodes directly.
- Instances run on nodes.

So an app can have multiple instances, and those instances can be distributed across different nodes.

Example:

- App `web-api`
- Instance `web-api-1` on node A
- Instance `web-api-2` on node B
- Instance `web-api-3` on node B

Nginx routes traffic to healthy instances, not to the app object itself.

## Setup

Requirements:

- Linux (Ubuntu, Debian, RHEL, Arch, openSUSE)
- Python 3.10+
- Git
- Docker
- Nginx
- systemd recommended

Install:

```bash
git clone https://github.com/Tonioot/Cloudbase
cd cloudbase
sudo bash install.sh
```

The installer configures dependencies, Python environment, Docker, nginx, systemd service, and Cloudbase CLI commands.

After install:

1. Save the generated admin password
2. Open `http://<server-ip>:7823`
3. Sign in

## Daily workflow

1. Create an app from a repository
2. Configure network settings (domain, SSL, DNS target)
3. Start with one instance
4. Scale by adding more instances
5. Optionally place instances on remote nodes
6. Monitor logs/stats and use zero-downtime restart for updates

## Nodes and clustering

Cloudbase supports a primary node with connected remote nodes.

- Primary node: control plane and central UI
- Remote nodes: workers that execute instance commands

Connect a remote node:

```bash
cloudbase connect --main-url <url> --invite-code <code>
```

Node modes:

- `panel+node`: full panel and node agent on same server
- `node-only`: worker mode for running instances only

## Operations and CLI

Core commands:

```bash
cloudbase start
cloudbase stop
cloudbase restart
cloudbase status
cloudbase logs
cloudbase update
cloudbase uninstall
```

Node commands:

```bash
cloudbase connect --main-url <url> --invite-code <code> --node-name <name> --mode <mode>
cloudbase disconnect
cloudbase node-status
```

Nginx and cert commands:

```bash
cloudbase nginx <domain>
cloudbase nginx show
cloudbase nginx disable
cloudbase nginx permissions [user]
cloudbase cert add <path> [name]
cloudbase cert list
cloudbase cert path
```

Backup and restore:

```bash
cloudbase export [file]
cloudbase import <file>
```

Cloudbase data is stored in `~/.cloudbase/`.

## Troubleshooting

- App reachable internally but not externally: verify the service binds to `0.0.0.0` inside its runtime.
- Node flaps online/offline: check network stability between node and primary and inspect `cloudbase logs`.
- Temporary missing stats during restarts: short gaps can occur during reconnect and recover automatically when the command pipeline stabilizes.

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

This means:
- You are free to use, modify, and distribute this software
- If you modify and distribute it, you must also open source your changes
- If you run this as a service, you must provide the source code to users

See the LICENSE file for full terms.

## Attribution & Origin

Cloudbase was originally created by Tonioot.

If you use or modify this project, you must:
- Retain the original copyright notice
- Provide proper attribution to the original author

This repository is considered the canonical upstream source of Cloudbase.