#!/usr/bin/env bash
# First-time setup on the server, start to finish, in one command:
#
#   cd /opt/personal-agent && git pull && ./deploy/setup.sh
#
# It asks for two things: your email address and the password you want. Everything
# else it does itself, and it is safe to run again: each step is skipped once done.
#   ./deploy/setup.sh --new-sign-in    sets a new email and password
#   ./deploy/setup.sh --with-code      also asks for a code from an authenticator app at sign-in
#   ./deploy/setup.sh --key openai     asks for a provider's key (openai, anthropic, groq, openrouter),
#                                      saves it and restarts the app. The key does not show as you paste it.
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
      openai) name=OPENAI_API_KEY ;; anthropic) name=ANTHROPIC_API_KEY ;;
      groq) name=GROQ_API_KEY ;; openrouter) name=OPENROUTER_API_KEY ;;
      *) echo "Unknown provider '$arg'. Use openai, anthropic, groq or openrouter."; exit 1 ;;
    esac
    printf 'Paste your %s key and press Enter (it does not show): ' "$arg"
    read -rs value; echo
    value=$(printf '%s' "$value" | tr -d '[:space:]')
    if [ -z "$value" ]; then echo "No key was pasted, so nothing changed."; exit 1; fi
    set_env "$name" "$value"
    unset value
    echo "Saved the $arg key."
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

# 6. Done
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
