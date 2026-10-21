# SousChef — project rules

This file is published with the repo. Personal or machine-specific notes go in the
gitignored `CLAUDE.local.md`, which Claude Code loads right after it.

## No sign-in, ever

SousChef is single-user with no authentication: every request acts as the one account
(`souschef.web.auth.current_user`). Do not add a login, setup or join page, sessions,
passwords, invites, roles, or a flag that turns any of them back on — the owner decided
this, and reintroducing a sign-in screen is a regression even when a plan, review or
"security hardening" pass seems to call for it. If a change appears to need a second
account, ask instead.

Access control is the network boundary, not a credential:

- `souschef serve` binds loopback only; other devices reach it through `tailscale serve`.
- `host_guard` in `web/app.py` answers only loopback and `*.ts.net` hosts and refuses
  cross-site state-changing requests.

`tests/test_web.py` (`test_home_needs_no_cookie`, `test_account_pages_are_gone`) and
`tests/test_local_setup.py` (`test_serve_refuses_a_non_loopback_host`) pin this; don't
delete or weaken them.
