#!/usr/bin/env bash
# First-time setup on the server, start to finish, in one command:
#
#   cd /opt/personal-agent && git pull && ./deploy/setup.sh
#
# It asks for two things: your email address and the password you want. Everything
# else it does itself, and it is safe to run again: each step is skipped once done.
#   ./deploy/setup.sh --new-sign-in    sets a new email and password
#   ./deploy/setup.sh --with-code      also asks for a code from an authenticator app at sign-in
#   ./deploy/setup.sh --key openai     asks for a service's key (openai, anthropic, groq, openrouter, canva, higgsfield),
#                                      saves it and restarts the app. The key does not show as you paste it.
#   ./deploy/restore.sh                returns the agent to one of its backups (a separate command)
#   ./deploy/setup.sh --local-model qwen2.5:3b
#                                      runs an open-weight model on this server for routine steps
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

# 1b. A provider's key, pasted here and nowhere else
set_env() {  # set_env NAME VALUE: replace the line in .env, or add it
  if grep -q "^#\? *$1=" .env; then
    sed -i "s|^#\? *$1=.*|$1=$2|" .env
  else
    echo "$1=$2" >> .env
  fi
}
LOCAL_MODEL=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--key" ]; then
    case "$arg" in
      openai) names="OPENAI_API_KEY" ;; anthropic) names="ANTHROPIC_API_KEY" ;;
      groq) names="GROQ_API_KEY" ;; openrouter) names="OPENROUTER_API_KEY" ;;
      canva) names="CANVA_CLIENT_ID CANVA_CLIENT_SECRET" ;; higgsfield) names="HIGGSFIELD_API_KEY" ;;
      *) echo "Unknown service '$arg'. Use openai, anthropic, groq, openrouter, canva or higgsfield."; exit 1 ;;
    esac
    for name in $names; do
      printf 'Paste the %s value %s and press Enter (it does not show): ' "$arg" "$name"
      read -rs value; echo
      value=$(printf '%s' "$value" | tr -d '[:space:]')
      if [ -z "$value" ]; then echo "Nothing was pasted for $name, so it was not changed."; exit 1; fi
      set_env "$name" "$value"
      unset value
    done
    echo "Saved for $arg."
  fi
  [ "$prev" = "--local-model" ] && LOCAL_MODEL="$arg"
  prev="$arg"
done

# 1c. An open-weight model on this server
if [ -n "$LOCAL_MODEL" ]; then
  free_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
  if [ "$free_mb" -lt 4500 ]; then
    echo "This server has ${free_mb} MB of memory free. A local model needs about 4,500 MB, and the other"
    echo "sites here share the machine, so the model was not started. Nothing changed."
    exit 1
  fi
  say "Starting Ollama and fetching $LOCAL_MODEL (a few minutes)"
  docker compose --profile local-models up -d ollama
  docker compose --profile local-models exec ollama ollama pull "$LOCAL_MODEL"
  set_env OLLAMA_API_BASE "http://ollama:11434"
  echo "Fetched. Choose it for a tier on the Settings and cost screen."
fi

# 2. Build
say "Building the app (a few minutes the first time)"
docker compose build api

# 3. Sign-in: asked for once, written to .env here, never printed
KEYS="PERSONAL_AGENT_OWNER_EMAIL PERSONAL_AGENT_OWNER_PASSWORD_HASH PERSONAL_AGENT_SESSION_SECRET PERSONAL_AGENT_TOTP_SECRET"
CODE_FLAG=""; NEW=""
for arg in "$@"; do
  [ "$arg" = "--with-code" ] && CODE_FLAG="--with-code" && NEW="yes"
  [ "$arg" = "--new-sign-in" ] && NEW="yes"
done
SETUP_KEY=""; SETUP_QR=""; SIGNED_UP=""
have=$(grep -cE '^PERSONAL_AGENT_(OWNER_EMAIL|OWNER_PASSWORD_HASH|SESSION_SECRET)=.+' .env || true)
if [ "$have" != "3" ] || [ -n "$NEW" ]; then
  say "Your sign-in: an email address and a password (12 characters or more). The password does not show as you type."
  mkdir -p data
  until docker compose run --rm --no-deps api sh -c "python -m personal_agent.auth $CODE_FLAG > /app/data/auth.txt" \
        && grep -q '^PERSONAL_AGENT_SESSION_SECRET=.' data/auth.txt; do
    echo
    echo "That did not work: $(head -n 1 data/auth.txt 2>/dev/null)"
    echo "Try again."
  done
  for key in $KEYS; do
    value=$(grep "^$key=" data/auth.txt | cut -d= -f2-)
    if grep -q "^$key=" .env; then
      sed -i "s|^$key=.*|$key=$value|" .env
    else
      echo "$key=$value" >> .env  # a settings file made before this line existed
    fi
  done
  SETUP_KEY=$(grep 'setup key:' data/auth.txt | sed 's/.*setup key: *//' || true)
  SETUP_QR=$(sed -n '/^Scan this with your authenticator app:/,$p' data/auth.txt | tail -n +2)
  SIGNED_UP=$(grep '^PERSONAL_AGENT_OWNER_EMAIL=' data/auth.txt | cut -d= -f2-)
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

# 6. The three scheduled jobs. One file of this app's own; no other site's schedule is touched.
if [ -d /etc/cron.d ]; then
  HERE=$(pwd)
  cat > /etc/cron.d/personal-agent <<CRON
# Written by $HERE/deploy/setup.sh. Times are the server's.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
15 2 * * * root $HERE/deploy/jobs.sh backup >/dev/null 2>&1
45 2 * * * root $HERE/deploy/jobs.sh retention >/dev/null 2>&1
0 6 * * 1 root $HERE/deploy/jobs.sh learning >/dev/null 2>&1
CRON
  chmod 644 /etc/cron.d/personal-agent
  say "Scheduled the daily backup, the daily retention job and the weekly review. Taking a first backup now"
  ./deploy/jobs.sh backup || echo "The first backup did not work. The site is still running; send the line above for help."
else
  echo "This server has no /etc/cron.d, so the backup and the other jobs were not scheduled."
fi

# 7. Done
say "Checking"
curl -fsS "https://$DOMAIN/health" && echo
echo
echo "Your site is live: https://$DOMAIN"
if [ -n "$SIGNED_UP" ]; then
  echo "Sign in with the email $SIGNED_UP and the password you chose."
fi
if [ -n "$SETUP_KEY" ]; then
  echo
  echo "You asked for a sign-in code as well. No code is ever sent to you: an app on your phone shows it."
  echo "Open Google Authenticator (or Microsoft Authenticator), tap +, choose 'Scan a QR code',"
  echo "and point the phone at this:"
  echo
  echo "$SETUP_QR"
  echo
  echo "If it will not scan, choose 'Enter a setup key' and type:  $SETUP_KEY"
fi
