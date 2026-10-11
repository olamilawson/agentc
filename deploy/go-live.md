# Going live on ijeoma.pro

You run these steps on the Hostinger VPS and in the Hostinger panel. The app
cannot do them for you. Work through them in order; each step says how to
confirm it.

## 1. How this server takes web traffic

This VPS serves several domains. Its own nginx holds ports 80 and 443 and
passes each domain to an app on a local port. The agent joins the same way: it
listens on `127.0.0.1:8100`, and one nginx site passes ijeoma.pro to it. The
Caddy service in `docker-compose.yml` stays off here. Nothing in these steps
names another domain.

Confirm the port is free. No output means it is.

```bash
ss -ltn | grep ':8100 '
```

If it is taken, add `API_PORT=<a free port>` to `.env` in step 5 and use that
port in the nginx file in step 9.

## 2. Point the domain at the server

In the Hostinger panel, open Domains, then ijeoma.pro, then DNS records.

- Add an `A` record: name `@`, value the VPS IPv4 address.
- Add an `A` record: name `www`, value the same address.
- Delete any `A`, `AAAA` or `CNAME` record on `@` or `www` that points
  somewhere else, such as a parking page.

Confirm from your own machine. Both names must answer with the VPS address.

```bash
nslookup ijeoma.pro
```

```bash
nslookup www.ijeoma.pro
```

## 3. Harden the server

Complete items 1 to 3 of `deploy/hardening.md` (firewall, key-based SSH,
security updates) before any client material goes on the server.

## 4. Put the code on the server

```bash
sudo mkdir -p /opt/personal-agent && sudo chown "$USER" /opt/personal-agent
git clone https://github.com/olamilawson/agentc.git /opt/personal-agent
cd /opt/personal-agent
```

A private repository needs a read-only deploy key for this repository; see
item 7 of `deploy/hardening.md`. Install Docker with the compose plugin if the
server lacks it.

## 5. Write the settings file

```bash
cp .env.example .env
chmod 600 .env
```

Fill in these lines in `.env`:

| Line | Value |
|---|---|
| `POSTGRES_PASSWORD` | a long random value, for example from `openssl rand -hex 32` |
| `PERSONAL_AGENT_NAME` | the name to show in the app |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | one key per provider you use |

Leave `PERSONAL_AGENT_DATABASE_URL` and `PERSONAL_AGENT_CHECKPOINT_DB` empty;
the compose file sets the database.

## 6. Create your sign-in

```bash
docker compose build api
docker compose run --rm --no-deps api python -m personal_agent.auth
```

The command asks for a password, then prints three lines and a setup key.
Paste the three lines into `.env`. Add the setup key to an authenticator app
on your phone, and store a copy of the key somewhere safe.

## 7. Check the model routes

`config/route_table.yaml` points all three tiers at OpenAI and names no
fallback. Its model names have not been checked against your account. Confirm
that your key can call each one, correct any that it cannot, commit, and push.
A run fails at its first model step if a name is wrong or the key is missing.

Add a `fallback:` line to a tier once a second provider's key is on the server.

## 8. Deploy

```bash
./deploy/deploy.sh
```

The script builds the image, tests it against Postgres, and starts the
database and the app if the test passes. It ends with "api is healthy". It
starts no web server and touches no port but 8100 on this machine.

```bash
curl -s http://127.0.0.1:8100/health
```

That prints `{"ok":true}`.

## 9. Add the nginx site and its certificate

```bash
cp /opt/personal-agent/deploy/nginx-ijeoma.pro.conf /etc/nginx/sites-available/ijeoma.pro
ln -s /etc/nginx/sites-available/ijeoma.pro /etc/nginx/sites-enabled/ijeoma.pro
nginx -t
```

`nginx -t` checks every site's configuration and changes nothing. Go on only
if it reports the test is successful.

```bash
systemctl reload nginx
certbot --nginx -d ijeoma.pro -d www.ijeoma.pro
```

A reload keeps the other sites up. certbot obtains the certificate and edits
this one site to serve HTTPS; choose the redirect to HTTPS if it asks.

Confirm:

- `https://ijeoma.pro/health` shows `{"ok":true}`.
- `https://ijeoma.pro` shows the sign-in page. Sign in with your password and a
  code from the authenticator app. Sign-in works over HTTPS and not before.
- Settings and cost lists your model routes.

## 10. Schedule the three jobs

```bash
sudo crontab -e
```

```
15 2 * * *  /opt/personal-agent/deploy/backup.sh
30 2 * * *  cd /opt/personal-agent && docker compose exec -T api python -m personal_agent.retention
0  6 * * 1  cd /opt/personal-agent && docker compose exec -T api python -m personal_agent.learning
```

`backup.sh` needs your age public key and an off-server destination first;
read the top of the script.

## Known limits on the first day

- **Mail has no connection until you set it up.** The agent holds approved
  sponsorship replies in a local outbox and sends none. `deploy/microsoft-graph.md`
  has the steps; a tenant administrator does the first four.
- **Accepted skill changes do not persist.** The image carries the skill files,
  so a change you accept on the Skill library screen disappears at the next
  deploy. Until the server mounts `skills/` from a git checkout, make skill
  changes in the repository.
- **The mail watcher is off** until you set `PERSONAL_AGENT_N8N_SECRET` and
  build the n8n workflow that posts to `/hooks/n8n/sponsorship`.
- **No live model run has happened yet.** Run one brief through the agent and
  read its trace on the Runs screen before you rely on it.
- **A lost phone locks you out of the app, not the server.** Repeat step 6 from
  SSH to set a new sign-in.
