# Deploying SousChef to an Oracle Always Free ARM VM

Target: `VM.Standard.A1.Flex`, 2 OCPU / 12 GB, Ubuntu 24.04. That is the current
Always Free allowance — Oracle [halved it from 4/24 on 15 June 2026](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
— and it is comfortably more than this app needs.

Everything below is plain Docker Compose, so if Oracle reclaims or refuses the
instance the same stack redeploys unchanged to any ~$5/mo VPS.

## 1. Instance

Create the VM with an Ubuntu 24.04 ARM image. If provisioning fails with "out of
host capacity", retry in a different availability domain or region — this is
common on the free tier and is not a fault in your account.

Open 80 and 443 in the VCN **security list**, then again on the instance itself:

```bash
sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Oracle's Ubuntu images ship a restrictive `iptables` chain **in addition to** the
cloud security list. Opening only the security list leaves the port silently
filtered, which looks exactly like a broken app.

## 2. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER" && newgrp docker
```

## 3. DNS

Point an A record at the instance's public IP. Certificates are issued by
Let's Encrypt on first boot, so DNS must resolve **before** you start the stack.

Real HTTPS is not cosmetic: without it the PWA will not install, the camera
input is restricted, and the session cookie cannot carry `Secure`.

**A free subdomain is enough.** At [duckdns.org](https://www.duckdns.org), sign
in, claim a name, and set its IP to the instance's public address. `duckdns.org`
is on the Public Suffix List, so cookies and Let's Encrypt rate limits are scoped
to your subdomain alone and no other user's name affects yours.

Confirm it resolves before going further — a certificate that fails to issue is
almost always this:

```bash
dig +short souschef-example.duckdns.org      # must print the instance IP
```

Swapping to a real domain later is an A record plus one line in `.env`; nothing
else changes.

**Don't put Cloudflare's proxy in front of this.** Cloudflare would terminate
TLS, which means your receipt images and pantry data pass through a third party
in plaintext. Caddy talking directly to Let's Encrypt keeps everything on your
own box. (Cloudflare *DNS only* — grey cloud — is fine.)

## 4. Getting the code and secrets onto the box

If the repo has a remote, clone it. If it only exists on your workstation, copy
it up instead — this skips the venv, database and secrets, all of which are
rebuilt or created on the box:

```bash
# From the workstation
rsync -az --filter=':- .gitignore' --exclude .git \
      ~/souschef/ ubuntu@<vm>:~/souschef/
```

Then, on the VM:

```bash
cd souschef
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
scp ubuntu@<vm>:~/souschef/.secrets/* ~/.secrets/canonical/souschef/
backup-secrets
```

## 5. Start

```bash
docker compose up -d --build     # first build takes ~10 min on 2 ARM cores
docker compose logs -f app
```

The image pre-downloads the OCR models and NLTK data at build time, so the first
receipt scan and first recipe import don't stall on a download.

Visit `https://your-domain` and **claim the instance**: the first visitor to an
empty database creates the admin account, and the claim page closes behind them.
Do this immediately after the stack comes up — until you do, anyone who finds the
hostname can claim it instead of you.

The first request creates the schema and loads the ingredient catalog.

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

There is no email-based password reset by design — shell access to this VM is
deliberately the only way back in.

## 6. Install on the phone

Open the site in Chrome on Android → menu → **Install app**. That registers
SousChef as a share target, so a NYT recipe or a receipt photo can be sent
straight to it from the system share sheet.

## 7. Backups

The `backup` service snapshots the database nightly to `./backups` using
SQLite's backup API (a plain file copy risks a torn read mid-write).

**On-box backups are worthless on a free tier that reclaims idle instances.**
Pull them somewhere else:

```bash
# From the workstation, via cron
rsync -az ubuntu@<vm>:~/souschef/backups/ ~/souschef-backups/
```

Restore:

```bash
docker compose stop app
gunzip -c backups/souschef-<stamp>.db.gz > /tmp/restore.db
docker compose cp /tmp/restore.db app:/data/souschef.db
docker compose start app
```

## 8. Keeping the instance

Oracle reclaims Always Free instances that look idle. The app's own health check
generates enough activity to count, but verify the instance is still running
after the first few weeks.

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
| Site unreachable, security list open | Instance `iptables` still closed — see step 1 |
| Certificate never issues | DNS not resolving yet, or 80/443 blocked |
| Sign-in succeeds then every page bounces to `/login` | Running plain HTTP behind a proxy that isn't sending `X-Forwarded-Proto` |
| Everyone is signed out at once | Someone changed their password (revokes only *their* sessions), or `SOUSCHEF_SESSION_SECRET` changed (revokes everyone's) |
| The site offers to "claim" itself again | The database was replaced or the volume was lost — restore a backup before anyone claims it |
| NYT imports start failing | Session cookie expired — paste a fresh one in Settings |
| Receipt scan is slow the first time | Model load; ~1 s thereafter |
| `/healthz` returns 503 | Read `problems` in the JSON body; usually a missing secret |

## Moving off Oracle

Nothing here is Oracle-specific. On any Ubuntu VPS: install Docker, clone, copy
`.secrets/`, point DNS, `docker compose up -d`. Restore the newest backup into
the `souschef_data` volume.
