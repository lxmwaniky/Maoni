# Maoni — Unfiltered Session Notes & Feedback Wall

A lightweight, real-time feedback wall built with **FastAPI**, **PostgreSQL**, **SQLAlchemy**, and **Alpine.js** to demonstrate deploying an application to **Google Compute Engine (Ubuntu 24.04 LTS)**—both on bare metal and with **Docker Compose** behind **Nginx**.

## Stack

| Layer | Tech |
| --- | --- |
| Backend | Python + FastAPI |
| Database | PostgreSQL 16 (SQLAlchemy ORM) |
| Realtime | Server-Sent Events + Postgres `LISTEN/NOTIFY` |
| Frontend | Server-rendered HTML, Alpine.js, Tailwind |
| Tooling | `uv` for dependency management |
| Deployment | Docker Compose (app + Postgres), GCP-ready |

## How realtime works

When a note is created or liked, the backend publishes a `notes_changed` event:

1. On Postgres, the write path issues `pg_notify`, so the event reaches **every** app process, even behind multiple workers or replicas.
2. A dedicated background connection runs `LISTEN maoni_notes` and fans the signal out over **Server-Sent Events** (`GET /api/notes/stream`) to all connected browsers.
3. Each browser refetches the feed the moment the signal lands — new notes and like counts appear in every open tab in milliseconds.

If the SSE connection drops (network hiccup, proxy timeout), the browser automatically reconnects and falls back to quiet 5-second polling, so the wall is never stale, it just degrades gracefully. No WebSocket infrastructure, no extra services.

---

## 1. Server Preparation (Ubuntu 24.04 LTS)

Update system packages and install core tools (`git`, `curl`):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ca-certificates
```

Clone the repository:

```bash
git clone https://github.com/lxmwaniky/maoni.git
cd maoni
```

---

## 2. Option A: Traditional Bare-Metal Deployment

### Install `uv` and PostgreSQL
```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Install and start PostgreSQL
sudo apt install -y postgresql postgresql-contrib
sudo systemctl enable --now postgresql

# Create database user and database
sudo -u postgres psql -c "CREATE USER maoni_user WITH PASSWORD 'maoni_pass';"
sudo -u postgres psql -c "CREATE DATABASE maoni_db OWNER maoni_user;"
```

### Configure & Run the Application
```bash
cp .env.example .env
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

### Run in the Background with `systemd`
```bash
sudo tee /etc/systemd/system/maoni.service << EOF
[Unit]
Description=Maoni FastAPI Service
After=network.target postgresql.service

[Service]
User=$USER
WorkingDirectory=$HOME/maoni
ExecStart=$HOME/.local/bin/uv run uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now maoni
```

---

## 3. Option B: Docker & Docker Compose Deployment

### Install Docker & Docker Compose (Ubuntu 24.04)
```bash
# Remove any conflicting old Docker repo files
sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.sources

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the Docker repository to Apt sources
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# Install Docker Engine and Compose plugin
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable Docker and allow current user to run Docker without sudo
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

### Start the Containers
*(If running the bare-metal `systemd` service, stop it first so port `8000` is free: `sudo systemctl disable --now maoni`)*

```bash
docker compose up -d --build
```

---

## 4. Nginx Reverse Proxy & HTTPS (Certbot)

### Install & Configure Nginx
```bash
sudo apt install -y nginx

sudo tee /etc/nginx/sites-available/maoni << 'EOF'
server {
    listen 80;
    server_name <YOUR_DOMAIN> _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Disable buffering so Server-Sent Events (SSE) stream immediately
        proxy_buffering off;
        proxy_cache off;
    }
}
EOF

# Disable default site and enable Maoni
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/maoni /etc/nginx/sites-enabled/

# Test configuration and restart Nginx
sudo nginx -t && sudo systemctl restart nginx
```

### Enable HTTPS with Certbot
Ensure your domain's DNS `A` record points to the VM's External IP and ports `80`/`443` are open in the GCP Firewall:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d inkq.app
```

---

## 5. Useful Debugging & Inspection Commands

```bash
# Check active listening ports
sudo ss -tulpn

# View live Nginx access logs
sudo tail -f /var/log/nginx/access.log

# Check Docker container status & logs
docker compose ps
docker compose logs -f

# Connect to PostgreSQL inside Docker
docker compose exec db psql -U maoni_user -d maoni_db
```

---

## 6. Server Hardening & Security Best Practices

Once your VM is live on the public internet, automated bots will start scanning it within minutes. Follow these steps to lock down your server:

### 1. Enable the Host Firewall (UFW)
Only expose **SSH (22)**, **HTTP (80)**, and **HTTPS (443)**. Never expose PostgreSQL (`5432`) or Uvicorn (`8000`) directly to the internet:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

### 2. Bind Docker Ports to `127.0.0.1` Only
⚠️ **Critical Docker Gotcha:** In `docker-compose.yml`, writing `ports: - "8000:8000"` modifies Linux `iptables` directly and **bypasses UFW**, exposing port `8000` to the entire internet even if UFW blocks it! Always bind internal container ports to localhost so only Nginx can reach them:

```yaml
# Inside docker-compose.yml
ports:
  - "127.0.0.1:8000:8000"
```

### 3. Install Fail2Ban (Block Brute-Force Bots)
`fail2ban` monitors `/var/log/auth.log` and automatically bans IP addresses that repeatedly fail SSH login attempts:

```bash
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban

# Check banned IPs in the SSH jail
sudo fail2ban-client status sshd
```

### 4. Harden SSH Access
Ensure password authentication and direct `root` login are disabled (GCP disables these by default, but always verify on any VPS):

```bash
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf << 'EOF'
PermitRootLogin no
PasswordAuthentication no
EOF

sudo systemctl reload ssh
```

### 5. Enable Automatic Security Updates
Configure Ubuntu to automatically install critical OS security patches in the background:

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

### 6. Lock Down Secret Files (`.env`)
Ensure your `.env` file cannot be read by other users on the system, and replace default workshop credentials (`maoni_pass`) with a strong password in production:

```bash
chmod 600 .env
```

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/notes?sort=newest/popular` | List notes (max 100) |
| `POST` | `/api/notes` | Create a note `{recipient, message, sender?}` |
| `POST` | `/api/notes/{id}/like` | Like a note |
| `GET` | `/api/notes/stream` | SSE feed of `notes_changed` events |
| `GET` | `/api/meta` | Hostname of the instance serving you (used in the footer) |
| `GET` | `/` | Single-page UI |

## License

MIT