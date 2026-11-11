# Running SousChef

Day-to-day operation: reaching it from a phone, where the data lives, backups and logs. The
quick start is in the [README](../README.md).

## From your phone

Run Tailscale on the machine that hosts SousChef and on the phone, then
`tailscale serve --bg --https=443 http://127.0.0.1:8000`. The phone reaches
`https://<machine>.<tailnet>.ts.net` over a real certificate, which is what
installing the app, the service worker, the share target and the clipboard
need. The server stays bound to loopback. `souschef serve` refuses any other
`--host`: other devices reach it through `tailscale serve`, which proxies to
loopback and presents the tailnet name.

A request is answered only when its Host is loopback or a `*.ts.net` name, so a
public DNS name pointed at the machine is refused. A state-changing request
whose `Origin` names a different host is refused too. Tailscale's `X-Forwarded-*`
headers are allowed; the proxy sets Host to the `ts.net` name.

## Accounts

One account, one kitchen: pantry, recipes, plan, shopping list and
the NYT cookie. The ingredient catalog is shared reference data about food.

The **Always have** list (`CanonicalIngredient.is_staple`) and **ingredients
typed in by hand** live on that catalog. Write to them directly.

## Other commands

```bash
souschef backup           # snapshot the database, keeping the last 14
souschef restore          # list snapshots; pass one to roll back to it
souschef rebuild          # recompute pantry levels by replaying the ledger
souschef config           # show effective configuration
souschef users            # list accounts
```

## Where your data lives

| Path | What |
|---|---|
| `data/souschef.db` | Everything: pantry, ledger, recipes, plans, accounts |
| `data/backups/` | Snapshots from `souschef backup` |
| `~/.config/souschef/` | The encryption key, deliberately not in the data directory |

`SOUSCHEF_DATA_DIR` moves the first two elsewhere.

```bash
souschef backup
souschef restore                                        # list what there is
souschef restore backups/souschef-20260818T193000Z.db   # roll back to one
```

Snapshots go through SQLite's backup API rather than a file copy, which would
risk a torn read mid-write. Restore replaces the live database, so stop the app
first; the database it replaces is kept beside it as `souschef.db.replaced`.

### Backing it with something other than this machine

Snapshots are written through a small storage interface
(`src/souschef/storage/`), and the local filesystem is the only backend today. A
Google Drive backend is one class implementing that interface.

What will not work is putting the live SQLite file on a synced drive. SQLite
needs byte-range locks and an fsync that means something, and a sync client
gives it neither — you get silent corruption rather than an error. The keys stay
out of the data directory for a related reason: a key that syncs to a third
party is a key you have given away.

## Logs

Everything goes to stdout in the terminal running the app, tagged with a request
id that is also returned in the `X-Request-ID` header — so a page that
misbehaved can be traced to every line it produced:

```
2026-07-27T11:35:29-0400 INFO  [31ce7e26] souschef.web: GET /shopping -> 200 (126ms)
```

Warnings are reserved for things worth acting on: a recipe page that could not
be read, a catalog gap that blocked a unit conversion.
`SOUSCHEF_LOG_LEVEL=DEBUG` for more.

Credentials are scrubbed by the formatter before anything is written, so logs
are safe to paste into an issue. `/healthz` returns 503 rather than 200 when the
encryption key is missing — an app that looks healthy while unable to decrypt
the stored NYT cookie is worse than one that is plainly down.
