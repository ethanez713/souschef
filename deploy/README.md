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

## 4. Secrets

```bash
git clone <your-repo> souschef && cd souschef
mkdir -p .secrets && chmod 700 .secrets

python3 -c "import secrets; print(secrets.token_hex(32))"     > .secrets/secret_key
python3 -c "import secrets; print(secrets.token_urlsafe(48))" > .secrets/session_secret
printf '' > .secrets/spoonacular_key          # optional; leave empty to disable discovery
chmod 600 .secrets/*

echo "SOUSCHEF_DOMAIN=souschef.example.com" > .env
```

`secret_key` encrypts the stored NYT session cookie. Losing it costs you one
re-paste of that cookie and nothing else — it protects no other data.

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

Visit `https://your-domain` and set a password. The first request creates the
schema and loads the ingredient catalog.

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

## Operational notes

| Symptom | Cause |
|---|---|
| Site unreachable, security list open | Instance `iptables` still closed — see step 1 |
| Certificate never issues | DNS not resolving yet, or 80/443 blocked |
| Sign-in succeeds then every page bounces to `/login` | Running plain HTTP behind a proxy that isn't sending `X-Forwarded-Proto` |
| NYT imports start failing | Session cookie expired — paste a fresh one in Settings |
| Receipt scan is slow the first time | Model load; ~1 s thereafter |

## Moving off Oracle

Nothing here is Oracle-specific. On any Ubuntu VPS: install Docker, clone, copy
`.secrets/`, point DNS, `docker compose up -d`. Restore the newest backup into
the `souschef_data` volume.
