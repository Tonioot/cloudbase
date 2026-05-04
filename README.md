# Cloudbase

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
