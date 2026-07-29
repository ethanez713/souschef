# Deploying SousChef

The stack is plain Docker Compose against a Linux box with a real filesystem —
Caddy terminates TLS, the app runs behind it, a sidecar snapshots the database.
Nothing below is tied to a particular provider; the same steps work on a
free-tier cloud VM, a €4/mo VPS, or a machine in your house.

## What the host has to provide

| Requirement | Why |
|---|---|
| **A persistent disk** | The database and receipt images live on the filesystem. A host that resets storage on redeploy silently discards the pantry. |
| **~1 GB RAM** (see below) | 600 MB steady state, plus swap headroom. |
| **Docker + Compose** | The whole deployment. |
| **Inbound 80 and 443** | Let's Encrypt has to reach it, and the PWA needs real HTTPS. |
| **A stable hostname** | Anything resolvable; a free dynamic-DNS subdomain is fine. |

That first row is the one that rules hosts out — see "Picking a host".

## How much machine this actually needs

Measured, not guessed (`ps -o rss` against the running container):

| State | Resident |
|---|---|
| Idle, just booted | ~100 MB |
| Serving every page | ~103 MB |
| After the first receipt scan (OCR model resident) | ~427 MB |
| After further scans | ~427 MB, flat |

The OCR model is loaded lazily on the first scan and then stays put, so **~430 MB
is the steady state** once anyone has scanned a receipt. With Caddy and the host
OS that is roughly **600 MB all-in**, and the disk footprint is a few hundred MB
of database and receipt images.

**Building the image is heavier than running it.** The build compiles wheels and
pre-downloads the OCR models, and it will thrash or OOM on a 1 GB shared-core
box. Give any small host ~2 GB of swap, or build elsewhere and pull — see
"Building on a bigger machine".

## Picking a host

Checked July 2026. Free tiers change without warning, so confirm the current
terms before committing, and check whether an offer is genuinely always-free or
a trial credit that expires.

### Free, with a real disk

| Option | Shape | Catch |
|---|---|---|
| **GCP `e2-micro` Always Free** | 1 shared vCPU (burstable), 1 GB RAM, 30 GB standard persistent disk | Only free in `us-west1`, `us-central1`, `us-east1`, and only on *standard* PD — not balanced or SSD. **Egress is 1 GB/month**, then billed. Needs a billing account on file. No end date. This is the closest thing left to a permanent free VM. |
| **Your own hardware + a tunnel** | Whatever you own | An old laptop, a Pi, or this desktop. Genuinely $0 and the disk is as big as you like; costs you uptime and electricity. See "Hosting it at home". |
| **AWS `t4g.small` free trial** | 2 ARM vCPU, 2 GB RAM | 750 h/month, but the trial **ends 31 Dec 2026**, and on accounts created after July 2025 storage draws down a 6-month credit balance rather than being free. Fine as a stopgap, not a home. |

Of these, **GCP `e2-micro` is the default recommendation**: no expiry date,
enough disk, and the only tight dimension (1 GB RAM) is solved with swap plus a
pre-built image. Its 1 GB/month egress cap is the thing to actually watch — fine
for a household serving HTML, not fine if you start serving receipt images to
the world.

### Cheap, if free stops being worth the friction

| Option | Price | Notes |
|---|---|---|
| **Hetzner `CAX11`** | ~€3.79/mo | 2 ARM vCPU, 4 GB, 40 GB. Comfortably more than this app needs; no capacity games, no reclamation policy. |
| **Hetzner `CX22`** | ~€3.79/mo | Same, x86, if you want to skip the multi-arch question entirely. |

### Ruled out

**Container PaaS free tiers — Render, Railway, Koyeb, Fly.io.** These are ruled
out by the disk requirement, not by price:

- **Koyeb** has no persistent disks at all; stateful workloads are expected to
  use an external database and object storage.
- **Railway**'s free plan is a one-time $5 credit with 0.5 GB of volume, then
  a monthly minimum.
- **Render** no longer offers free persistent storage to new accounts.
- **Fly.io** discontinued its free allowance for new organisations in 2024;
  volumes are $0.15/GB-month.

Running on any of them means moving to Postgres plus object storage, which is a
real rewrite and not worth it for a kitchen app.

## 1. The box

Create a VM with a current Ubuntu or Debian image, or use hardware you own.
Whatever the host, give it swap if it has under 2 GB of RAM:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Firewall

Most clouds have **two** layers, and both must be open — one alone leaves the
port silently filtered, which looks exactly like a broken app.

**The provider's firewall.** GCP: a VPC firewall rule allowing `tcp:80,443` from
`0.0.0.0/0` to the instance (`gcloud compute firewall-rules create allow-web
--allow tcp:80,tcp:443`). Other providers put the same thing under "security
group", "security list", or "cloud firewall". Hetzner and most small VPS hosts
ship with it wide open and nothing to do here.

**The host's own firewall.** If `ufw` is active:

```bash
sudo ufw allow 80,443/tcp
```

Some cloud images instead ship a raw iptables ruleset ending in REJECT, where
appending does nothing — insert at the top and persist:

```bash
sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Verify from your workstation once the stack is up:

```bash
nc -zv <public-ip> 80 443
```

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
```

## 4. DNS

Point an A record at the box's public IP. Certificates are issued by Let's
Encrypt on first boot, so DNS must resolve **before** you start the stack.

Real HTTPS is not cosmetic: without it the PWA will not install, the camera
input is restricted, and the session cookie cannot carry `Secure`.

**A free subdomain is enough.** At [duckdns.org](https://www.duckdns.org), sign
in, claim a name, and set its IP to the box's public address. `duckdns.org` is on
the Public Suffix List, so cookies and Let's Encrypt rate limits are scoped to
your subdomain alone and no other user's name affects yours.

Confirm it resolves before going further — a certificate that fails to issue is
almost always this:

```bash
dig +short souschef-example.duckdns.org      # must print the box's IP
```

Swapping to a real domain later is an A record plus one line in `.env`; nothing
else changes.

**Don't put Cloudflare's proxy in front of this.** Cloudflare would terminate
TLS, which means your receipt images and pantry data pass through a third party
in plaintext. Caddy talking directly to Let's Encrypt keeps everything on your
own box. (Cloudflare *DNS only* — grey cloud — is fine.)

## 5. Getting the code and secrets onto the box

The repo is private, so the box needs read access. Use a **deploy key**: it is
read-only and scoped to this one repository, so a compromised host cannot push to
it or touch anything else in the account — unlike a personal access token or a
`gh auth login` on the box.

```bash
# On the box
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_deploy -C "souschef-host"
cat ~/.ssh/id_deploy.pub
```

```bash
# On the workstation, paste that public key in:
gh repo deploy-key add /dev/stdin --repo ethanez713/souschef --title "souschef-host"
```

Then, on the box:

```bash
git -c core.sshCommand="ssh -i ~/.ssh/id_deploy" \
    clone git@github.com:ethanez713/souschef.git
cd souschef
git config core.sshCommand "ssh -i ~/.ssh/id_deploy"   # so `git pull` keeps working

mkdir -p .secrets && chmod 700 .secrets

python3 -c "import secrets; print(secrets.token_hex(32))"     > .secrets/secret_key
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > .secrets/session_secret
printf '' > .secrets/spoonacular_key          # optional; leave empty to disable discovery
chmod 600 .secrets/*

echo "SOUSCHEF_DOMAIN=souschef-example.duckdns.org" > .env
```

`secret_key` encrypts the stored NYT session cookie. Losing it costs you one
re-paste of that cookie and nothing else — it protects no other data.

`session_secret` signs session cookies. Changing it signs **everyone** out at
once, which is the blunt instrument if you ever think a cookie has leaked.

Back the directory up off the box:

```bash
# On the WSL workstation, using the canonical vault
mkdir -p ~/.secrets/canonical/souschef
scp <user>@<host>:~/souschef/.secrets/* ~/.secrets/canonical/souschef/
backup-secrets
```

## 6. Start

```bash
docker compose up -d --build     # first build takes ~10 min on two small cores
docker compose logs -f app
```

The image pre-downloads the OCR models and NLTK data at build time, so the first
receipt scan and first recipe import don't stall on a download.

Visit `https://your-domain` and **claim the instance**: the first visitor to an
empty database creates the admin account, and the claim page closes behind them.
Do this immediately after the stack comes up — until you do, anyone who finds the
hostname can claim it instead of you.

The first request creates the schema and loads the ingredient catalog.

### Building on a bigger machine

On a 1 GB shared-core host the build is the painful part, not the app. Build on
your workstation and have the box pull the result:

```bash
# On the workstation (set SOUSCHEF_IMAGE in .env on both sides)
export SOUSCHEF_IMAGE=ghcr.io/ethanez713/souschef:latest
docker build --platform linux/amd64 -t "$SOUSCHEF_IMAGE" .   # or linux/arm64
echo "$GHCR_TOKEN" | docker login ghcr.io -u ethanez713 --password-stdin
docker push "$SOUSCHEF_IMAGE"
```

```bash
# On the box
echo "SOUSCHEF_IMAGE=ghcr.io/ethanez713/souschef:latest" >> .env
docker login ghcr.io -u ethanez713            # private package; read scope is enough
docker compose pull && docker compose up -d
```

Match `--platform` to the host's architecture — x86 for GCP `e2-micro` and most
VPS, arm64 for Hetzner `CAX` and AWS Graviton. Without `SOUSCHEF_IMAGE` set,
Compose just builds locally as before.

### Adding other people

Accounts are invite-only; there is no public registration form. From Settings →
Invites, generate a single-use link (`/join?code=…`, valid 7 days) and send it to
them. They pick their own username and password, so no password ever passes
through a chat app.

Each account is a separate kitchen: its own pantry, recipes, meal plan, shopping
list, receipts and NYT cookie. Nothing is shared between accounts except the
ingredient catalog itself.

From the box, if you would rather not use the web UI:

```bash
docker compose exec app souschef users            # who has an account
docker compose exec app souschef invite --note X  # mint an invite link
docker compose exec app souschef passwd alice     # the lost-password recovery path
```

There is no email-based password reset by design — shell access to this box is
deliberately the only way back in.

## 7. Install on the phone

Open the site in Chrome on Android → menu → **Install app**. That registers
SousChef as a share target, so a NYT recipe or a receipt photo can be sent
straight to it from the system share sheet.

## 8. Backups

The `backup` service snapshots the database nightly to `./backups` using
SQLite's backup API (a plain file copy risks a torn read mid-write).

**A backup that only exists on the box is not a backup.** Pull them somewhere
else:

```bash
# From the workstation, via cron
rsync -az <user>@<host>:~/souschef/backups/ ~/souschef-backups/
```

Restore:

```bash
docker compose stop app
gunzip -c backups/souschef-<stamp>.db.gz > /tmp/restore.db
docker compose cp /tmp/restore.db app:/data/souschef.db
docker compose start app
```

Treat the box as replaceable — that is the assumption the whole stack is built
on. With `.secrets/` in the vault and a recent backup pulled down, rebuilding
anywhere is steps 1–6 again.

## Hosting it at home

If you would rather not have a cloud account at all, the app is small enough to
run on any always-on machine you own. Two things change:

**Exposure.** Rather than forwarding ports on your router, a tunnel gets you a
public HTTPS URL from behind NAT. [Tailscale
Funnel](https://tailscale.com/kb/1223/funnel) is the one that fits this app's
threat model: TLS terminates on *your* machine, so the tunnel provider never sees
receipt images or pantry data in plaintext. The cost is a
`<host>.<tailnet>.ts.net` hostname — no custom domain on the free plan. A
Cloudflare Tunnel gives you a custom domain instead, but terminates TLS at
Cloudflare's edge, which is exactly what step 4 says not to do.

**Caddy's role.** With Funnel handling TLS, drop the `caddy` service and publish
the app on localhost instead — Funnel forwards to `127.0.0.1:8000`. Keep
`--forwarded-allow-ips` as it is; sessions still need `X-Forwarded-Proto`.

Under WSL2 specifically, note that the VM does not survive a Windows reboot
unless you set it to, and its IP changes — Funnel run *inside* WSL needs the
service re-established after a restart. A spare laptop running Linux directly is
the less fiddly version of this.

## Debugging a running instance

Logs go to stdout, so `docker compose logs` is the whole interface — there are
no log files to hunt for and nothing to rotate.

```bash
docker compose logs -f app                  # follow
docker compose logs app | grep -E 'WARNING|ERROR'   # just the problems
docker compose logs app --since 1h
```

Every line carries a **request id**:

```
2026-07-27T11:35:29-0400 INFO  [31ce7e26] souschef.ocr: OCR receipt.png: 40 boxes -> 14 lines, confidence 0.95, 0.8s
2026-07-27T11:35:29-0400 INFO  [31ce7e26] souschef.receipt: Receipt 1 (SUPERGROCER): 9 items, 8 auto-matched, 1 need review
2026-07-27T11:35:29-0400 INFO  [31ce7e26] souschef.web: POST /receipts/upload -> 303 (1469ms)
```

The same id is returned in the `X-Request-ID` response header, so a page that
misbehaved can be traced to every log line it produced:

```bash
docker compose logs app | grep 31ce7e26
```

Raise or lower verbosity without a rebuild:

```bash
docker compose exec -e SOUSCHEF_LOG_LEVEL=DEBUG app true   # or set it in compose.yaml
```

**Credentials are redacted before anything is written.** Cookies, tokens, API
keys and passwords are scrubbed by the formatter, so logs are safe to copy into
an issue. Do not defeat this by adding `print()` calls.

### What the warnings mean

| Log line | What to do |
|---|---|
| `OCR found no text in …` | Blank or badly blurred photo; retake it |
| `Low-confidence scan of … (0.4x)` | Poor light or a creased receipt; review each line carefully |
| `most lines … need manual review` | Normal for a new store; the alias table learns as you confirm |
| `no quantity and no package size for 'x'` | Catalog gap — give that ingredient a `package_qty` in `data/seeds/ingredients.json` |
| `%d of %d ingredient lines did not match the catalog` | Those lines are invisible to pantry checks until aliased |
| `Locked out <ip> after 8 failed sign-ins` | Someone is guessing a password |
| `Stored NYT cookie could not be decrypted` | `SOUSCHEF_SECRET_KEY` changed; re-enter the cookie |
| `POST /… SLOW (>3000ms)` | Expected once after a restart (OCR model load); investigate if repeated |

### Health check

`/healthz` returns `503 {"status": "degraded", …}` — not 200 — when the database
is unreachable or a required secret is missing. A container that answers 200
while unable to issue sessions is worse than one that is plainly down, because
nothing surfaces the problem until you try to sign in. Docker restarts on this.

## Operational notes

| Symptom | Cause |
|---|---|
| Site unreachable, provider firewall open | The host's own `ufw`/`iptables` is still closed — see step 2 |
| Certificate never issues | DNS not resolving yet, or 80/443 blocked |
| Sign-in succeeds then every page bounces to `/login` | Running plain HTTP behind a proxy that isn't sending `X-Forwarded-Proto` |
| Everyone is signed out at once | Someone changed their password (revokes only *their* sessions), or `SOUSCHEF_SESSION_SECRET` changed (revokes everyone's) |
| The site offers to "claim" itself again | The database was replaced or the volume was lost — restore a backup before anyone claims it |
| NYT imports start failing | Session cookie expired — paste a fresh one in Settings |
| Receipt scan is slow the first time | Model load; ~1 s thereafter |
| Build gets OOM-killed | No swap on a 1 GB box — see step 1, or build elsewhere |
| `/healthz` returns 503 | Read `problems` in the JSON body; usually a missing secret |

## Shipping an update

```bash
git pull
docker compose up -d --build          # or: docker compose pull && docker compose up -d
```

Schema changes apply themselves: the container runs `souschef init` on start,
which is `alembic upgrade head` followed by a catalog re-seed. **Take a backup
first anyway** — a migration is the one routine operation that can lose data:

```bash
docker compose exec app sh -c 'sqlite3 /data/souschef.db ".backup /data/pre-upgrade.db"'
docker compose cp app:/data/pre-upgrade.db ./backups/
```

## Moving to a different host

Install Docker, clone, copy `.secrets/` back from the vault, point DNS at the new
IP, `docker compose up -d`, then restore the newest backup into the
`souschef_data` volume. The only per-host details are the firewall (step 2) and
the CPU architecture of a pre-built image.
