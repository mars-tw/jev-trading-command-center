# Security

## Default local boundary

`python -B run_app.py` and `python -B run_app.py --demo --port 8765` are
loopback-only, offline DEMO modes. Market values, paper-account results, model
replies and agent activity are synthetic. Port 8766 is also loopback-only and
its optional companion is not started by default. DEMO does not require an API
key, contact an exchange, invoke Jev or launch a native AI tool.

Real public-market paper research requires the explicit `--real-paper` flag
and a process-environment `TYPESAFE_API_KEY`. Optional companion and native-tool
features require their own explicit flags. They can make external requests and
consume provider quotas. The application contains a `PaperBroker`, not a
private exchange-order executor, and no research result is evidence of profit.

Do not bind ports 8765, 8766 or the password gateway directly to a LAN or the
Internet. The supported remote boundary is a trusted HTTPS reverse proxy on
the same host forwarding to the gateway's loopback listener with one exact
`--public-origin`. The gateway does not depend on Tailscale and never accepts
`Forwarded`, `X-Forwarded-User` or another proxy-supplied identity as login
proof.

## Owner authentication and remote gateway

Create the only owner through the one-time loopback setup endpoint:

```sh
python -B run_remote.py --setup-owner --auth-store '<private-dir>/owner.json'
```

The auth store must remain outside the repository in a private directory with
an owner-only protected DACL. Setup refuses to overwrite an existing store.
Passwords are 8 through 128 Unicode code points and are stored with a unique
salt and scrypt; there is no default password or public signup.

The default gateway remains an offline DEMO:

```sh
python -B run_remote.py --public-origin https://public.example.invalid --auth-store '<private-dir>/owner.json'
```

The only supported real read-only bridge is explicit:

```sh
python -B run_remote.py --public-origin https://public.example.invalid --auth-store '<private-dir>/owner.json' --upstream http://127.0.0.1:8765 --companion http://127.0.0.1:8766 --market-analysis --enable-antigravity
```

`--market-analysis` requires a REAL upstream; combining it with DEMO is
rejected. Remote financial, scheduler, strategy and order-changing POST routes
remain disabled.

## Session and request controls

- Pre-authentication and authenticated sessions use opaque `__Host-` cookies
  with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/` and no `Domain`.
- Authenticated sessions expire after 30 minutes idle or 8 hours absolute.
  Expiry and browser back/forward-cache restoration mask the entire document,
  including panels added after initial load.
- Pre-authentication actions and every authenticated POST require their
  matching CSRF proof and exact Origin. Logout succeeds only after a confirmed
  valid request, then revokes the session.
- Login and route-specific rate limits, bounded request bodies, strict JSON,
  duplicate critical-header and cookie rejection, canonical route checks, and
  HTTP framing checks fail closed. Ambiguous request targets, paths, body
  lengths or transfer framing are rejected.
- Responses containing authenticated state are `no-store`; the dashboard,
  bootstrap, scripts and local dependencies are unavailable before login.

## Research-data and native-proof boundary

The analysis snapshot GET is cache-only and never fetches market data. Only an
owner-authenticated, CSRF-protected symbol-selection POST may fill the bounded
eight-source public Binance cache. It accepts closed bars for the canonical ten
timeframes. The 45-minute frame is exactly three closed 15-minute bars, the
16-hour frame is exactly two closed 8-hour bars, and aggregation is aligned to
UTC epochs. `60m` and `1h` are one frame; `24h` is `1d`.

Duplicate or out-of-order bars, gaps, partial or insufficient data, and a
missing latest expected closed bar produce `UNKNOWN`. The gateway does not
sort, fill, rescue or mark live data as closed. Source, actual and expected
as-of times, cache TTL and data staleness remain explicit. Results are
`research_only` and `analysis_only`, with `execution_authority=false`; they do
not create Jev evidence, change a strategy or authorize an order.

`--enable-antigravity` only prepares a public ten-frame research context. A new
native research request is denied before process spawn until tool, MCP, plugin
and credential isolation is proven. Existing AGY companion status may report a
real PID, job and fresh heartbeat through a bounded same-origin GET, but a
configured model, display label or fabricated PID is never provider-activity
proof. Quota and cooldown use bounded enums, and an unknown model remains
`UNVERIFIED`. This release does not promise real provider inference.

## Secrets and local state

- Supply secrets through the process environment or an OS secret manager.
- Never put credentials in examples, JSON, source, screenshots, recordings,
  issues, Git history or model prompts.
- Keep auth stores, `config.json`, logs, state, databases and native CLI
  credentials private and outside published artifacts. A `.gitignore` is not a
  secret scan.
- Native AI CLIs are separately installed third-party programs with their own
  authentication and terms. Do not share their home directories or transcript
  caches with this project.

## Reporting

Do not paste a working secret or private account data into a public issue. If a
credential was exposed, revoke or rotate it. Report a minimal redacted
reproduction through the repository's available security channel; external
providers and model services retain their own security policies.
