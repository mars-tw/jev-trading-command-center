# Security

## Default boundary

The default application is a loopback-only, offline DEMO. Its market values,
paper account, model replies and agent activity are synthetic. It does not
require API keys, contact an exchange, call Jev, or launch native AI tools.

Real public-market paper research requires the explicit `--real-paper` flag
and a process-environment `TYPESAFE_API_KEY`. Native companion work requires
its separate explicit opt-in. These modes can make external requests and use
provider quotas. The application includes a PaperBroker, not an exchange
private-order executor.

Do not expose the local HTTP server directly to the Internet. Control APIs
check loopback Host, same-origin requests, JSON content and bounded bodies.
Jev decisions are checked for valid types, finite probabilities, current
plan identity and one-time use. Those checks do not turn a probability or a
DEMO result into a claim of profitability.

## Secrets and local state

- Supply secrets through the process environment or an OS secret manager.
- Do not put real credentials in `.env.example`, JSON configuration, source,
  screenshots, recordings, issue reports, Git history or model prompts.
- Keep `config.json`, logs, state, databases and native CLI credentials local.
  The supplied `.gitignore` excludes these runtime artifacts.
- A native CLI is a separately installed third-party program with its own
  authentication and terms. Do not share its home directory or transcript
  cache with this project.
- Before publishing a fork or recording, use the offline DEMO and inspect
  the staged files and final frames. A `.gitignore` alone is not a secret scan.

## Reporting

Please do not paste a working secret or private account data into a public
issue. If a credential was exposed, revoke or rotate it with the provider.
Report a minimal, redacted reproduction through the repository's available
security reporting channel; otherwise open an issue without exploit secrets
or personal data. Only the published source and release assets are in scope;
external providers and their model services have separate policies.
