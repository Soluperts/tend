# Security policy

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security-sensitive reports.

Email: **ridhwan@soluperts.ca**

Include enough detail to reproduce the issue: tend version, platform, and the smallest test case you can produce.

I aim to acknowledge reports within **7 days** and to ship a fix or document a workaround within **30 days** for critical issues. For lower-severity findings the cadence depends on impact and complexity; I'll keep you posted.

## Supported versions

While tend is pre-1.0, only the **latest released version** receives fixes. Once 1.0 ships, this section will list the supported version range.

## Trust boundaries (background for reports)

- The wake gate is the privacy boundary: while Brain is deactivated, no STT audio leaves the device.
- The webhook receiver binds to `127.0.0.1:7331` only and authenticates with a per-install bearer token plus HMAC-SHA256 signature (see `docs/conventions.md` → "Webhook security").
- Secrets live in the OS keyring (preferred) or `$TEND_HOME/.env` with mode 600 (fallback for headless Linux).

Reports that exercise these boundaries are especially welcome.
