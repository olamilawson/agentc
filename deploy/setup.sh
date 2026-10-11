#!/usr/bin/env bash
# First-time setup on the server, start to finish, in one command:
#
#   cd /opt/personal-agent && git pull && ./deploy/setup.sh
#
# It asks for one thing: the password you want (twice). Everything else it does
# itself, and it is safe to run again: each step is skipped once it is done.
#   ./deploy/setup.sh --new-sign-in    makes a new password and authenticator key
set -euo pipefail

cd "$(dirname "$0")/.."
DOMAIN="ijeoma.pro"
SITE="/etc/nginx/sites-available/$DOMAIN"
say() { printf '\n== %s\n' "$1"; }

# 1. Settings file and database password
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi
if ! grep -q '^POSTGRES_PASSWORD=.\+' .env; then
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 32)|" .env
fi

# 2. Build
say "Building the app (a few minutes the first time)"
docker compose build api

# 3. Sign-in: asked for once, written to .env here, never printed
SETUP_KEY=""
have=$(grep -cE '^PERSONAL_AGENT_(OWNER_PASSWORD_HASH|TOTP_SECRET|SESSION_SECRET)=.+' .env || true)
if [ "$have" != "3" ] || [ "${1:-}" = "--new-sign-in" ]; then
  say "Choose your password (12 characters or more). Nothing shows while you type."
  mkdir -p data
  until docker compose run --rm --no-deps api sh -c 'python -m personal_agent.auth > /app/data/auth.txt' \
        && grep -q '^PERSONAL_AGENT_SESSION_SECRET=.' data/auth.txt; do
    echo
    echo "That did not work: $(grep -v '^This sets' data/auth.txt 2>/dev/null | head -n 1)"
    echo "Try again."
  done
  for key in PERSONAL_AGENT_OWNER_PASSWORD_HASH PERSONAL_AGENT_TOTP_SECRET PERSONAL_AGENT_SESSION_SECRET; do
    value=$(grep "^$key=" data/auth.txt | cut -d= -f2-)
    sed -i "s|^$key=.*|$key=$value|" .env
  done
  SETUP_KEY=$(grep 'setup key:' data/auth.txt | sed 's/.*setup key: *//')
  SETUP_QR=$(sed -n '/^Scan this with your authenticator app:/,$p' data/auth.txt | tail -n +2)
  rm -f data/auth.txt
fi

# 4. Start the database and the app (deploy.sh tests the build first)
say "Starting the app"
./deploy/deploy.sh

# 5. Connect the domain through this server's nginx. Only this one site is added.
say "Connecting $DOMAIN"
if [ ! -d /etc/nginx/sites-enabled ]; then
  echo "This server's nginx does not use /etc/nginx/sites-enabled, so the domain was not connected."
  echo "The app itself is running. Send this message to whoever is helping you set up."
  exit 1
fi
added=""
if [ ! -e "/etc/nginx/sites-enabled/$DOMAIN" ]; then
  cp "deploy/nginx-$DOMAIN.conf" "$SITE"
  ln -s "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
  added="yes"
fi
if ! nginx -t; then
  # Leave nginx exactly as it was: the other sites must keep reloading cleanly.
  if [ -n "$added" ]; then rm -f "/etc/nginx/sites-enabled/$DOMAIN" "$SITE"; fi
  echo "nginx reported a problem with its configuration, so nothing was changed. Send the lines above for help."
  exit 1
fi
systemctl reload nginx

if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  if ! command -v certbot >/dev/null; then
    echo "certbot is not installed, so the site has no certificate yet. Send this message for help."
    exit 1
  fi
  say "Getting the certificate (certbot may ask for an email address the first time)"
  certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --redirect
fi

# 6. Done
say "Checking"
curl -fsS "https://$DOMAIN/health" && echo
echo
echo "Your site is live: https://$DOMAIN"
if [ -n "$SETUP_KEY" ]; then
  echo
  echo "One thing left, on your phone. No code is ever sent to you: an app on your phone shows it."
  echo "Open Google Authenticator (or Microsoft Authenticator), tap +, choose 'Scan a QR code',"
  echo "and point the phone at this:"
  echo
  echo "$SETUP_QR"
  echo
  echo "If it will not scan, choose 'Enter a setup key' and type:  $SETUP_KEY"
  echo
  echo "The app now shows a six-digit code that changes every 30 seconds."
  echo "To sign in: your password, plus the code showing at that moment."
  echo "Write the setup key down somewhere safe, and share it with no one."
fi
