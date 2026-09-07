# Deploying to a server

The assistant is designed to run unattended. Once it is on a server it no longer
depends on your workstation being awake, online, or healthy.

Everything below assumes a fresh Ubuntu 24.04 box.

---

## What you need

| | Recommendation | Cost |
|---|---|---|
| Server | **Hetzner CX22** — 2 vCPU, 4 GB RAM, 40 GB disk | ~€3.79/month |
| | *or* Vultr / DigitalOcean **Singapore**, 2 GB plan — closer to Cambodia | ~$10–12/month |
| Domain | Cloudflare Registrar (sold at cost) | ~$10/year |
| | *or* a free **DuckDNS** subdomain | free |

**Sizing.** The bot itself is light: the language model runs remotely on Ollama
Cloud, so the server only marshals messages and talks to APIs. MongoDB is the
memory-hungry part and wants about 1 GB. 2 GB total works; 4 GB is comfortable
and leaves room for the weekly PowerPoint generation.

**Why a domain is not optional.** Google refuses plain HTTP and bare IP
addresses as OAuth redirect URIs. Without a hostname you cannot complete
`/connectgoogle`, so Calendar and Gmail stay disconnected. A DuckDNS subdomain
satisfies this perfectly well if you would rather not buy a domain yet.

---

## 1. Create the server

Pick Ubuntu 24.04 and **add your SSH public key during creation** — do not
choose password login. If you do not have a key yet, on your own machine:

```bash
ssh-keygen -t ed25519 -C "assistant-server"
cat ~/.ssh/id_ed25519.pub     # paste this into the provider's SSH key field
```

Then connect:

```bash
ssh root@YOUR_SERVER_IP
```

## 2. Point the domain at it

Create a DNS **A record** for the hostname you want (for example
`assistant.example.com`) pointing at the server's IPv4 address. If you are using
DuckDNS, create a subdomain there and set its IP.

Confirm it resolves before continuing — Caddy cannot issue a certificate until
it does:

```bash
dig +short assistant.example.com
```

## 3. Basic server hardening

```bash
apt update && apt upgrade -y
apt install -y ufw fail2ban

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# Refuse SSH passwords; keys only
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# Unattended security updates
apt install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
```

Note that **3141 is never opened**. The dashboard is only reachable through
Caddy over HTTPS.

## 4. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
docker --version && docker compose version
```

## 5. Get the code

```bash
git clone -b uat https://github.com/cvrvai/telegrambot.git /opt/assistant
cd /opt/assistant
```

## 6. Create the secrets

**Every value must be newly generated.** Nothing from the old workstation
should be reused — assume all of it is known to someone else.

```bash
cp .env.example .env
nano .env
```

Regenerate each of these at its source:

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — my.telegram.org
- `TELEGRAM_BOT_TOKEN` — @BotFather → `/revoke`, then `/token`
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — Google Cloud Console (create new credentials, delete the old ones)
- `OLLAMA_API_KEY` — new key from your Ollama account
- `DASHBOARD_TOKEN` — generate one: `openssl rand -base64 32`

Set these to the real hostname:

```ini
DOMAIN=assistant.example.com
DASHBOARD_PUBLIC_URL=https://assistant.example.com
GOOGLE_OAUTH_REDIRECT_URI=https://assistant.example.com/oauth/google/callback
```

`DOMAIN` is what Caddy reads to request the certificate; the other two must
match it exactly or the OAuth callback will not return.

## 7. Register the redirect URI with Google

In Google Cloud Console → **APIs & Services → Credentials → your OAuth client →
Authorised redirect URIs**, add exactly:

```
https://assistant.example.com/oauth/google/callback
```

It must match `GOOGLE_OAUTH_REDIRECT_URI` character for character, including the
scheme and any trailing path. A mismatch produces `redirect_uri_mismatch`.

## 8. First run — log the userbot in

The userbot signs in as *you*, so Telegram sends a login code that has to be
typed once. Run the container interactively for this:

```bash
docker compose build
docker compose run --rm -it bot python main.py run
```

Enter the code Telegram sends you (and your 2FA password if you have one). Once
you see `Telethon Userbot connected`, press **Ctrl+C**.

The session is saved to `./runtime/` on the host, so this is a one-time step —
later restarts reuse it.

## 9. Start it properly

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose logs -f bot
```

Caddy will fetch a Let's Encrypt certificate on first start. That takes a few
seconds; if it fails, DNS almost certainly has not propagated yet.

## 10. Verify

1. `https://assistant.example.com/webapp?token=YOUR_DASHBOARD_TOKEN` loads over HTTPS
2. Message the bot on Telegram — it replies
3. Send `/connectgoogle`, complete the consent screen, confirm it reports connected
4. Ask it something that uses Calendar, e.g. *"what's on my calendar this week?"*

---

## Backups

MongoDB holds the situations, briefs and message history. Back it up nightly:

```bash
cat > /etc/cron.daily/assistant-backup <<'EOF'
#!/bin/sh
cd /opt/assistant || exit 1
docker compose exec -T mongo mongodump --archive --db telegram_business \
  | gzip > "/opt/backups/assistant-$(date +%F).gz"
find /opt/backups -name 'assistant-*.gz' -mtime +14 -delete
EOF
mkdir -p /opt/backups
chmod +x /etc/cron.daily/assistant-backup
```

Also back up `./runtime/` — it holds the Telegram session files. Treat that
directory as a credential: anyone with it can act as your Telegram account.

## Updating

```bash
cd /opt/assistant
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## Optional: authenticate MongoDB

Mongo is not published to the host or the internet — only the bot container can
reach it over the compose network. Adding credentials is defence in depth if you
later run anything else on the same box:

```yaml
# docker-compose.prod.yml
  mongo:
    environment:
      MONGO_INITDB_ROOT_USERNAME: assistant
      MONGO_INITDB_ROOT_PASSWORD: ${MONGO_PASSWORD:?set MONGO_PASSWORD}
  bot:
    environment:
      MONGO_URI: mongodb://assistant:${MONGO_PASSWORD}@mongo:27017/?authSource=admin
```

Do this on a fresh volume, or existing data will not be readable under the new
credentials.

---

## Switching from Ollama to Claude

The demo runs on Ollama so it costs nothing to show. Production runs on Claude.
Both are built into the same image -- switching is three lines of `.env` and a
restart, with no code change and no rebuild.

```ini
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-opus-5
```

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose logs bot | grep "Business AI provider"
```

That log line reports the provider and model actually in use -- check it rather
than assuming the switch took.

**Cost control.** Claude bills per token, so the budget guardrail stops mattering
in theory and starts mattering in practice:

```ini
AI_MONTHLY_BUDGET_USD=20
```

Pricing for known models is compiled in ($5.00 / $25.00 per million tokens for
`claude-opus-5`), so spend is tracked accurately without you configuring rates.
If you set `ANTHROPIC_MODEL` to something this build does not recognise, it
refuses to start rather than billing it as free -- set
`AI_INPUT_PRICE_PER_MILLION` and `AI_OUTPUT_PRICE_PER_MILLION` for that case.

`ANTHROPIC_EFFORT` (`low`, `medium`, `high`, `xhigh`, `max`) trades depth against
cost and latency. Leave it blank for the API default; `low` or `medium` is worth
measuring if replies feel slow, since most assistant turns are routine.

**Going back to the demo** is the same switch in reverse -- set
`AI_PROVIDER=ollama` and restart. Nothing else changes, so you can keep a UAT
box on Ollama and production on Claude from one branch.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Caddy cannot get a certificate | DNS not resolving yet, or ports 80/443 blocked |
| `redirect_uri_mismatch` | `GOOGLE_OAUTH_REDIRECT_URI` differs from the URI registered in Google Cloud |
| Bot starts then exits | Usually a missing `.env` value — check `docker compose logs bot` |
| Asks for a login code on every restart | `./runtime/` is not persisting; check the volume mount |
| `SSLCertVerificationError` on Google APIs | A proxy is intercepting TLS. On a clean server this should never appear — see `docs/` history for what it meant on the old workstation |
