# Cloudbase

**Original Cloudbase project by Tonioot (2026)**

Cloudbase is a self-hosted deployment and operations platform for managing applications across multiple servers.

It provides a central control plane for deploying applications from Git, distributing workloads across instances, and operating a multi-node infrastructure with integrated networking, load balancing, and observability.

---

## Philosophy

Cloudbase is designed with a focus on simplicity and clarity.

The system prioritizes:

* A minimal and understandable operational model
* Explicit control over infrastructure behavior
* Predictable deployment and scaling semantics
* Avoidance of unnecessary complexity in orchestration

The goal is to keep multi-server application management transparent and easy to reason about.

---

## Overview

Cloudbase runs on port 7823 by default and consists of a control plane and distributed execution nodes.

Key capabilities include:

* Deployment of applications from Git repositories
* Docker-based runtime for application execution
* Instance-based scaling across multiple servers
* Centralized orchestration of multi-node environments
* Automated NGINX configuration per application
* Domain management and SSL certificate handling
* Load balancing across healthy instances
* Live logging, metrics, and audit events
* Role-based access control

---

## System Architecture

Cloudbase is built around a primary node architecture with optional remote nodes.

### Primary Node (Control Plane)

The primary node is the central system component. It hosts the Cloudbase panel and is responsible for:

* Storing all application configuration and metadata
* Managing nodes and their state
* Orchestrating deployments and updates
* Handling routing decisions and load balancing configuration
* Serving as the single entry point for user interaction

The primary node can also function as a regular node and host application instances like any other node.

All control operations are managed through the primary node.

### Nodes (Execution Layer)

Nodes are servers that execute workloads. Each node must have Cloudbase installed.

Nodes are responsible for:

* Running application instances
* Hosting Docker containers for workloads
* Reporting status and health to the primary node
* Receiving deployment and runtime instructions

---

## Core Model: App, Instance, Node

Cloudbase uses three core abstractions.

### Application

An application represents a logical service definition.

It does not execute workloads itself. Instead, it defines configuration such as:

* Git repository
* Build and start commands
* Internal service port
* Environment variables
* Domain and routing configuration
* Resource limits

The primary node stores the application source code state. When a Pull and Rebuild operation is executed, the latest code is pulled and stored on the primary node.

### Instance

An instance is a running execution of an application.

* Each instance runs as an isolated Docker container
* Each instance has its own runtime environment
* Instances can be distributed across multiple nodes
* At least one instance is required for an application to be active

### Node

A node is a physical or virtual machine that runs instances.

---

## Networking and Routing

Cloudbase uses NGINX as the primary routing layer.

* Each application can define one or more domains
* SSL certificates can be configured per domain
* Traffic is automatically distributed across healthy instances
* Load balancing is handled by Cloudbase via NGINX configuration generation

When instances run on remote nodes, Cloudbase establishes secure tunnels between the primary node and remote nodes. All external traffic is routed through the primary node and forwarded through these tunnels to the appropriate node and instance.

If a node goes offline, all tunnels to that node are terminated and its instances are marked as disconnected. Remaining instances automatically handle traffic through load balancing.

When the node comes back online, Cloudbase restores the tunnel connection. If the instance still exists, the container is restarted and reattached to the routing system, after which load balancing resumes automatically.

---

## Application Lifecycle Pages

Cloudbase provides system-managed NGINX-served pages for application states.

### Downtime

Displayed when an application is not reachable via NGINX or explicitly set to downtime mode from the panel.

### Update

Displayed when an application is in an update state triggered manually via the panel.

### Restart

Displayed automatically during application restarts and removed once the application becomes healthy again.

### Start

Displayed during initial application startup. Similar to restart but specifically for first boot or cold start.

Each lifecycle page uses a standard template with configurable fields. Alternatively, full custom HTML can be used for complete control over rendering.

All lifecycle pages are served via NGINX and managed internally by Cloudbase.

---

## Technology Stack

Cloudbase uses the following core technologies:

* Docker for instance isolation and execution
* NGINX for routing, SSL termination, and traffic management
* Python-based control plane for orchestration and CLI
* Git for source code retrieval and deployments

## Setup

### Requirements

* Linux (Ubuntu, Debian, RHEL, Arch, openSUSE)
* Python 3.10+
* Git
* Docker
* NGINX
* systemd (recommended)

---

### Installation

```bash
git clone https://github.com/Tonioot/Cloudbase
cd cloudbase
sudo bash install.sh
```

The installer configures:

* System dependencies
* Python environment
* Docker
* NGINX
* systemd services
* Cloudbase CLI 

---

### After installation

* Store the generated admin password securely
* Open the panel at: `http://<server-ip>:7823`
* Log in to the control panel

---

## Node Management

### Primary node

The primary node hosts the control panel and manages orchestration.

### Remote nodes

Remote nodes execute application instances.

### Connect a node

```bash
cloudbase connect --main-url <url> --invite-code <code>
```

### Node modes

* panel+node: combined control plane and execution node
* node-only: execution-only worker node

---

## CLI Operations

### Core commands

```bash
cloudbase start
cloudbase stop
cloudbase restart
cloudbase status
cloudbase logs
cloudbase update
cloudbase uninstall
```

### Node commands

```bash
cloudbase connect --main-url <url> --invite-code <code> --node-name <name> --mode <mode>
cloudbase disconnect
cloudbase node-status
```

### NGINX management

```bash
cloudbase nginx <domain>
cloudbase nginx show
cloudbase nginx disable
cloudbase nginx permissions [user]
```

### Certificates

```bash
cloudbase cert add <path> [name]
cloudbase cert list
cloudbase cert path
```

### Backup and restore

```bash
cloudbase export [file]
cloudbase import <file>
```

---

## Data Storage

Cloudbase stores all persistent data in:

```
~/.cloudbase/
```

---

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

You are free to use, modify, and distribute this software under the terms of the license.
If you modify and distribute the software, you must publish your changes.
If you run it as a service, you must provide source code access to users.

See the LICENSE file for full terms.

---

## Attribution

Cloudbase was originally created by Tonioot.

If you use or modify this project, you must retain attribution and preserve the original copyright notice.

This repository is considered the canonical upstream source of Cloudbase.
