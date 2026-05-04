# Cloudbase

Cloudbase is een self-hosted deployment panel waarmee je applicaties kunt deployen, beheren en monitoren vanaf een webinterface.

Het platform is gemaakt voor teams die snel willen kunnen schalen over meerdere servers, met Docker-first deployments, nginx reverse proxying, node clustering, live logs/stats en ingebouwde operationele tooling.

## 1. Wat is Cloudbase?

Cloudbase draait standaard op poort 7823 en biedt:

- Centrale dashboard voor al je apps en nodes
- Deployments vanuit Git repositories
- Docker containers en optioneel native process mode
- Multi-node cluster beheer vanuit 1 hoofdpanel
- Instance-based scaling per app
- Zero-Downtime restart flow (ZD Restart)
- Nginx per app met domains, redirects en SSL
- Maintenance/update/restarting/starting pagina's
- Audit logs, role-based access en user management

## 2. Belangrijke begrippen

### App
Een applicatie in Cloudbase. Een app bevat onder andere:

- Repo URL
- Start command
- Internal port
- Domeinen/SSL instellingen
- Environment variabelen
- Runtime settings (CPU/memory/read-only/tmpfs)

### Node
Een server die opdrachten uitvoert. Je hebt:

- Primary/Main node: centrale coordinator
- Remote node(s): gekoppelde workers in de cluster

Nodes communiceren met de primary via WebSocket + agent commands.

### Instance
Een draaiende replica van een app.

- Elke instance heeft een eigen external host port
- Instances kunnen lokaal of op remote nodes draaien
- Bij remote nodes loopt verkeer via reverse tunnel naar de primary
- Nginx load-balanced over alle gezonde instances

## 3. Architectuur in het kort

- Backend: FastAPI (Python 3.10+)
- Frontend: Vanilla JS/HTML/CSS
- Database: SQLite
- Runtime: Docker (standaard) + native fallback
- Proxy: nginx per app
- Realtime: WebSockets voor logs, stats en node events

Data staat onder `~/.cloudbase/`.

## 4. Systeemvereisten

- Linux (Ubuntu, Debian, RHEL, Arch, openSUSE)
- Python 3.10+
- Git
- Nginx
- Docker
- systemd (aanbevolen voor autostart/servicebeheer)

## 5. Installatie en eerste setup

```bash
git clone https://github.com/Tonioot/Cloudbase
cd cloudbase
sudo bash install.sh
```

De installer regelt automatisch:

- Systeempakketten
- Python venv + dependencies
- Docker Engine setup
- Nginx setup
- Systemd service
- Cloudbase CLI commando's

Na installatie:

1. Noteer het admin wachtwoord (wordt eenmalig getoond)
2. Open `http://<server-ip>:7823`
3. Log in als admin

Belangrijk: op systemen waar cgroup memory nog niet aan staat, activeert de installer dit automatisch. Een reboot kan nodig zijn.

## 6. Snelle operationele workflow

1. Deploy app via repository
2. Configureer domain/SSL in Settings > Network
3. Voeg instances toe (lokaal of op remote nodes)
4. Controleer logs/stats
5. Gebruik ZD Restart voor updates zonder zichtbare downtime

## 7. App lifecycle en schaalbaarheid

### Deploy en run

- Docker-first deployment
- Auto detectie van app type (Node, Python, Ruby, Go, PHP, Java, .NET)
- Build en run met detectie/default start command

### Scaling met instances

- Scale per app via extra instances
- Per instance eigen external port
- Instances verspreid over nodes mogelijk
- Runtime metrics per instance (CPU, geheugen, netwerk)

### Zero-Downtime Restart

ZD Restart doet in grote lijnen:

1. Nieuwe image/build voorbereiden
2. Nieuwe instances starten en health-checken
3. Nginx atomisch omzetten naar gezonde nieuwe backends
4. Oude instances netjes afbouwen

### Restart policies

- `no`: geen automatische restart
- `always`: altijd herstarten
- `on-failure`: alleen bij failure, met backoff

## 8. Nginx, domains, DNS en SSL

Per app kun je instellen:

- Primary domain
- Extra domains
- Redirect domains (301 naar primary)
- SSL cert + key

Cloudbase genereert nginx config inclusief:

- Reverse proxy headers
- WebSocket upgrade headers
- Load balancing over actieve instances

Er is ook een DNS Setup flow in de app pagina die uitlegt welk public IP je moet gebruiken en hoe traffic daarna automatisch over instances verdeeld wordt.

## 9. Multi-node cluster

Cluster opzetten:

1. Maak invite code op de primary
2. Run op remote server:

```bash
cloudbase connect --main-url <url> --invite-code <code>
```

3. Node verschijnt in het panel
4. Deploy of scale instances naar die node

Beschikbare node modes:

- `panel+node` (default): panel + agent op dezelfde machine
- `node-only`: alleen agent/local API voor worker nodes

## 10. Monitoring, logs en audit

### Logs

- Live app logs
- Replica logs
- Node agent logs

### Stats

- App-level stats
- Instance-level stats
- Node-level health metrics (CPU/memory/disk)

### Audit

- Centrale audit events voor app, auth, users, nodes en operations
- Filterbaar op actor/action

## 11. Gebruikers en rechten

Rollen:

- `admin`: volledige toegang
- `viewer`: read-only toegang

Admin kan users beheren via de UI.

## 12. CLI overzicht

### Core

```bash
cloudbase start
cloudbase stop
cloudbase restart
cloudbase status
cloudbase logs
cloudbase enable
cloudbase disable
cloudbase update
cloudbase uninstall
```

### Account

```bash
cloudbase password
```

### Nodes

```bash
cloudbase connect --main-url <url> --invite-code <code> --node-name <name> --mode <mode>
cloudbase disconnect
cloudbase node-status
```

### Nginx

```bash
cloudbase nginx <domain>
cloudbase nginx show
cloudbase nginx disable
cloudbase nginx permissions [user]
```

### Certificaten

```bash
cloudbase cert add <path> [name]
cloudbase cert list
cloudbase cert path
```

### Backup/restore

```bash
cloudbase export [file]
cloudbase import <file>
```

## 13. Data en opslag

Cloudbase gebruikt `~/.cloudbase/`:

| Pad | Inhoud |
|---|---|
| `cloudbase.db` | Apps, instances, nodes, instellingen |
| `credentials` | Gehashte credentials |
| `secret_key` | Signing key |
| `certs/` | SSL certificaten |
| `apps/` | App broncode/checkouts |
| `logs/` | App logs, service logs, agent logs |

## 14. Updaten

Aanbevolen:

```bash
cloudbase update
```

Of handmatig:

```bash
cd /path/to/cloudbase
git pull
cloudbase restart
```

## 15. Troubleshooting

### App draait maar is niet bereikbaar

Controleer of je service in container op `0.0.0.0` bindt (niet op `127.0.0.1`).

### Node lijkt online/offline te flippen

Check:

- `cloudbase logs`
- netwerkstabiliteit tussen node en primary
- node agent process status

### Stats ontbreken tijdelijk

Cloudbase gebruikt zowel stream- als poll-fallbacks. Tijdens restarts of reconnects kan er kort een gat zitten, maar metrics vullen weer aan zodra node/replica command flow stabiel is.
