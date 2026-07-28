# Deploying SousChef to an Oracle Always Free ARM VM

Target: `VM.Standard.A1.Flex`, Ubuntu 24.04. The Always Free allowance is **2 OCPU
/ 12 GB** across all A1 instances in the tenancy, since Oracle
[halved it from 4/24 on 15 June 2026](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
— still far more than this app needs. Ask for a fraction of it; §"How much machine
this actually needs" explains why the small shape is the better request.

Everything below is plain Docker Compose, so if Oracle reclaims or refuses the
instance the same stack redeploys unchanged to any ~$5/mo VPS.

## How much machine this actually needs

Measured, not guessed (`ps -o rss` against the running container):

| State | Resident |
|---|---|
| Idle, just booted | ~100 MB |
| Serving every page | ~103 MB |
| After the first receipt scan (OCR model resident) | ~427 MB |
| After further scans | ~427 MB, flat |

The OCR model is loaded lazily on the first scan and then stays put, so **~430 MB
is the steady state** once anyone has scanned a receipt. With Caddy and Ubuntu
that is roughly **600 MB all-in**, and the disk footprint is a few hundred MB of
database and receipt images.

### Ask for 1 OCPU / 3 GB, not 2 OCPU / 12 GB

Counter-intuitively, the *small* shape is the better ask on all three axes:

- **Capacity.** The scheduler is hunting for a host with room for what you
  requested. A smaller request is a much easier fit, and `Out of host capacity`
  is the binding constraint on this tier — not the allowance.
- **Idle reclamation.** Oracle deems an Always Free instance idle only if CPU,
  network **and** memory are all under 20% across 7 days (see §8). At ~600 MB
  used, a 12 GB box sits at 5% memory and is a reclamation candidate; a 3 GB box
  sits near 25% and is not. Right-sizing is the honest way to stay under that
  policy — no fake load required.
- **Trial end.** Staying well inside 2 OCPU / 12 GB total means nothing to unwind
  when the trial expires (see below).

Give it ~2 GB of swap so the image build, which is heavier than the running app,
has headroom.

## 1. Instance

Create the VM with an Ubuntu 24.04 ARM image.

**`Out of host capacity` is the normal experience, not a fault in your account.**
Free-tier A1 capacity is genuinely scarce in popular regions. In rough order of
effectiveness:

1. **Ask for less.** Request **1 OCPU / 3 GB** rather than 2/12 — see above for
   why the small shape is better on every axis, not just this one.
2. **Rotate availability domains** (AD-1, AD-2, AD-3) and omit the fault domain.
3. **Retry on a loop.** Capacity frees up unpredictably; a script that retries
   every few minutes generally succeeds within hours where clicking the console
   does not.
4. **Take the x86 micro instead.** `VM.Standard.E2.1.Micro` (1 GB, AMD) is also
   Always Free, you get two, and it almost always has capacity. It fits the 600 MB
   above — but it is **1/8 OCPU burstable**, so `docker compose build` is
   painfully slow. Build the image elsewhere and push it to a registry, or expect
   receipt scans to take tens of seconds rather than two.

### If you leave a retry loop running unattended

Check the **Instances** list when you come back, before doing anything else:

- **Delete any duplicates.** A loop that keeps clicking after a success can
  provision more than one. Within the Always Free allowance the spares are free;
  on a Pay As You Go account, anything past the allowance **bills you**.
- **Confirm the instance is marked "Always Free Eligible."** A shape or boot
  volume outside the free limits is billable, and during the 30-day trial it will
  look fine right up until the trial ends.

Nothing is at risk from simply not returning for a few hours — a fresh instance
exposes only key-only SSH, with 80/443 still closed and nothing installed yet.

## 1b. Firewall (after the instance exists)

Neither firewall layer is configurable on the create-instance form. Both are set
afterwards, and neither needs a rebuild.

**VCN security list** — Networking → Virtual Cloud Networks → your VCN →
Security Lists → Default Security List → *Add Ingress Rules*. Two stateful rules,
source CIDR `0.0.0.0/0`, IP protocol TCP, destination port `80` and `443`. It
takes effect within seconds; the instance keeps running.

**The instance's own iptables** — SSH in and run:

```bash
sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

`-I` inserts at the top; `-A` would append *after* the REJECT rule Oracle's
Ubuntu images ship with, which does nothing. Both layers must be open — one alone
leaves the port silently filtered, which looks exactly like a broken app.

Verify from your workstation once the stack is up:

```bash
nc -zv <public-ip> 80 443
```

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

The repo is private, so the VM needs read access. Use a **deploy key**: it is
read-only and scoped to this one repository, so a compromised VM cannot push to
it or touch anything else in the account — unlike a personal access token or a
`gh auth login` on the box.

```bash
# On the VM
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_deploy -C "souschef-oracle"
cat ~/.ssh/id_deploy.pub
```

```bash
# On the workstation, paste that public key in:
gh repo deploy-key add /dev/stdin --repo ethanez713/souschef --title "oracle-vm"
```

Then, on the VM:

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

## 8. Staying free, and keeping the instance

Verified against Oracle's
[Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
docs, 28 July 2026. Confirm before trusting it — Oracle
[halved the A1 allowance in June 2026](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
with no announcement, just a documentation edit.

| Resource | Always Free allowance |
|---|---|
| Ampere A1 compute | 1,500 OCPU-hours + 9,000 GB-hours/month — i.e. **2 OCPU / 12 GB** running continuously |
| AMD compute | **2 ×** `VM.Standard.E2.1.Micro`, 1/8 OCPU + 1 GB each |
| Block + boot volume | **200 GB total**, plus 5 backups |
| Outbound data | **10 TB/month** |

This app needs 1 OCPU, 3 GB, one ~50 GB boot volume and a rounding error of
egress, so it sits far inside every line.

### Reclamation of idle instances

> Oracle will deem virtual machine and bare metal compute instances as idle if,
> during a 7-day period, the following are true:
> - CPU utilization for the 95th percentile is less than 20%
> - Network utilization is less than 20%
> - Memory utilization is less than 20% *(applies to A1 shapes only)*

Two things follow.

**Being away for hours or days is irrelevant.** The test is sustained low
utilization over a week, not recent logins.

**All three conditions must hold — so memory is the lever.** An earlier version of
this file claimed the health check generated enough activity to count; it does
not, and a kitchen app used twice a week will not clear the CPU bar either. But it
does not have to: at ~600 MB resident, a **3 GB** instance sits around 25% memory
and therefore fails the "idle" test outright, while a 12 GB instance sits at 5%
and qualifies. Sizing the box to the workload is what keeps it, not fake load.

Oracle does not document how it measures memory utilization, so leave margin
rather than aiming exactly at 20%.

### At the end of the 30-day trial

Always Free resources keep running; the account stays open. The trap is
**over-provisioning during the trial**, when the $300 credit hides it: if you hold
more A1 than the Always Free allowance when the trial ends, *all* your A1
instances are disabled and deleted 30 days later unless you upgrade to a paid
account. Staying at 1 OCPU / 3 GB means there is nothing to unwind.

**Don't upgrade to Pay As You Go to dodge reclamation.** Earlier advice in this
file said to; that was wrong if you want a guaranteed $0. A paid tenancy gets the
same 1,500 OCPU-hour A1 grant, but anything past it — or past 200 GB of storage —
is billed rather than refused, which converts a hard limit into a bill. Always
Free refusing to provision is the behaviour you want.

Keep backups off the box (§7) and treat the instance as replaceable — that is the
assumption the whole stack is built on.

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

## Shipping an update

```bash
git pull
docker compose up -d --build
```

Schema changes apply themselves: the container runs `souschef init` on start,
which is `alembic upgrade head` followed by a catalog re-seed. **Take a backup
first anyway** — a migration is the one routine operation that can lose data:

```bash
docker compose exec app sh -c 'sqlite3 /data/souschef.db ".backup /data/pre-upgrade.db"'
docker compose cp app:/data/pre-upgrade.db ./backups/
```

## Moving off Oracle

Nothing here is Oracle-specific. On any Ubuntu VPS: install Docker, clone, copy
`.secrets/`, point DNS, `docker compose up -d`. Restore the newest backup into
the `souschef_data` volume.

**The one requirement that rules hosts out is a persistent disk.** This app keeps
its database and receipt images on the filesystem, so any platform with an
ephemeral one — the free tiers of Render, Railway, Koyeb and friends, where a
redeploy or a restart resets the disk — would silently discard the pantry.
Running there means moving to Postgres plus object storage, which is a real
rewrite and not worth it for a kitchen app.

Known-good alternatives, both giving a real VM with a real disk:

| Option | Notes |
|---|---|
| **GCP `e2-micro`** always-free, `us-west1`/`us-central1`/`us-east1` | 1 GB RAM + 30 GB disk, free indefinitely. Fits the 600 MB above; add ~1 GB of swap for headroom during the image build. The closest true equivalent to Oracle. |
| **Hetzner `CAX11`** (~€3.79/mo) | 2 ARM vCPU, 4 GB, 40 GB. Not free, but it is the same architecture as the A1 and the stack moves across untouched. |

Free-tier terms change often; confirm the current allowance before committing to
one, and check whether it is genuinely "always free" or a trial credit that
expires.
