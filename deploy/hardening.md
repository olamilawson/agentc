# Server hardening before any client material is loaded (PRD: server)

The control panel currently shows no firewall rules and two snapshots with no
automated daily backup. Every item below is done on the VPS before the first
run touches real briefs. Nothing here is executed by the application.

## 1. Firewall — inbound on SSH, HTTP, HTTPS only

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

## 2. SSH — key login, root login disabled

```bash
# Add your key first and confirm a second terminal can log in, then:
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
```

## 3. Automatic security updates

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

## 4. Internal services stay off the public internet

The compose file binds Postgres to the internal Docker network only (no
published ports) and exposes the app solely through Caddy. The n8n editor must
sit behind login and an address allowlist (host firewall or reverse proxy rule)
— n8n keeps only the mailbox watcher and never sends mail.

## 5. Backups — daily, encrypted, off the server, test-restored monthly

`backup.sh` (run from cron as root) dumps Postgres, the client folders and the
Caddy data, encrypts with the owner's age public key, and ships it off the
server. A restore is rehearsed monthly into a scratch directory and the result
is checked before it counts.

## 6. Secrets

One key per provider, in the server environment or a secrets file outside the
repository. Keys never go into prompts, never into git, never into the UI.

## 7. Records

Note the date each item above was completed in `docs/adr/` alongside the
Foundation gate result: firewall verified, restore verified.
