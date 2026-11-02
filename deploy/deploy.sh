#!/usr/bin/env bash
# Deploy GrantForge on a fresh Ubuntu 22.04/24.04 server (any provider: DigitalOcean, Hetzner, Linode, AWS Lightsail…).
#
# Before running:
#   1. Point your domain's DNS A record (e.g. grants.example.com) at this server's public IP.
#   2. Copy this repository to the server, or let this script clone it: REPO_URL=https://github.com/<you>/grantforge.git
#   3. Have your .env ready (see .env.example). It must set GF_DOMAIN, GF_BASE_URL=https://<GF_DOMAIN>,
#      GF_UI_PASSWORD, ANTHROPIC_API_KEY, GF_NOTIFY_TO and the SMTP settings.
#
# Run as root (or with sudo):   bash deploy/deploy.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/grantforge}
REPO_URL=${REPO_URL:-}

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker"
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

if [ ! -d "$APP_DIR" ]; then
  if [ -n "$REPO_URL" ]; then
    git clone "$REPO_URL" "$APP_DIR"
  else
    mkdir -p "$APP_DIR"
    cp -r "$(cd "$(dirname "$0")/.." && pwd)"/. "$APP_DIR"/
  fi
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "No .env in $APP_DIR. Copy .env.example to .env, fill it in, and run this script again." >&2
  exit 1
fi
for var in GF_DOMAIN GF_BASE_URL GF_UI_PASSWORD ANTHROPIC_API_KEY GF_NOTIFY_TO; do
  if ! grep -Eq "^${var}=.+" .env; then
    echo "$var is missing or empty in .env" >&2
    exit 1
  fi
done
# The app sits behind Caddy here, so it may trust Caddy's X-Forwarded-For header.
if grep -q '^GF_TRUST_PROXY=' .env; then sed -i 's/^GF_TRUST_PROXY=.*/GF_TRUST_PROXY=true/' .env; else echo 'GF_TRUST_PROXY=true' >> .env; fi

# Firewall: SSH, HTTP (certificate challenge and redirect) and HTTPS only.
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
fi

docker compose --profile https up -d --build
echo "GrantForge is starting at $(grep '^GF_BASE_URL=' .env | cut -d= -f2-)"
echo "Logs: docker compose logs -f grantforge"
echo "Update later: git pull && docker compose --profile https up -d --build"
