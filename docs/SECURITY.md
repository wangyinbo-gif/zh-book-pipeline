# Security

Status: **v1.0.7 — unified session authentication shipped** (contract v1). The server remains
localhost-first. It is **not** designed for direct public-internet exposure; LAN/reverse-proxy
use must enable authentication and prefer HTTPS or a trusted VPN.

## Threat model

- Default bind `127.0.0.1` (open mode, no auth — the documented local default).
- User data: `data/progress.json`, `data/bookmarks.json`, `data/practice.json`; book content
  served only from `books/`. Reading is treated as sensitive too: book texts, personal notes,
  progress and question banks are all behind the same gate when auth is enabled.
- The former LAN helper script (`平板访问.bat`) was retired on 2026-11-26; remote reading is
  expected via a static offline site instead of exposing `server.js`.

## Architecture of the protection (contract v1)

- Single credential (`AUTH_TOKEN`) → server-side session → one global authorization gate for
  **all** routes (API, assets, static pages), not per-route token checks.
- `POST /api/auth/login` (timing-safe compare; `HttpOnly` `SameSite=Strict` cookie
  `zhuxue_sid`, random 64-hex sid; response carries a 32-hex CSRF token; per-IP rate limit
  10 failures/min → 429), `POST /api/auth/logout` (revokes session, clears cookie),
  `GET /api/auth/status` (anonymous, returns only `{authed, mode, csrf?}`).
- Sessions are in-memory with absolute expiry (`SESSION_TTL`, default 8h). Restart invalidates
  all sessions. Single-process deployment only. Credentials never appear in logs.
- Gate order (locked by tests): Host allow-list 400 → anonymous-path method binding 405 →
  open-mode passthrough → unauthenticated: 401 for API, 302 to `/login.html?next=` for
  navigational GET (Accept: text/html; next is same-origin validated) → authenticated writes:
  CSRF token + Origin/Referer same-origin or 403 → business dispatch.
- Static and asset handlers accept GET/HEAD only (else 405). Leading-slash variants are
  normalized before classification so `//api/...` cannot slip the whitelist. PATCH counts as
  a write method. Anonymous whitelist is exactly 5 entries (`/login.html`, three auth endpoints,
  `/api/health` with a minimal `{ok, mode}` body).
- **Host allow-list (anti DNS-rebinding, P0 fix, 2026-11-26)**: only loopback names, the bound
  host and local interface addresses are accepted; custom domains/mDNS names must be declared
  via `ALLOW_EXTRA_HOSTS` (comma-separated) or requests are rejected with 400 before any
  session check.

## Deployment modes

- **A. Local (default)**: `node server.js` on 127.0.0.1; auth optional. Set `AUTH_TOKEN` to
  require login; set `AUTH_ENFORCE=1` to force login on localhost (with an empty token the
  server refuses to start, exit code 79, by design).
- **B. LAN / behind a reverse proxy**: `AUTH_TOKEN` mandatory — non-loopback bind without a
  token exits with code 78 and prints generation instructions (`crypto.randomBytes`). Prefer
  HTTPS or a trusted VPN (plain HTTP LAN leaks credentials to same-network devices). With a
  trusted proxy set `TRUST_PROXY=1` (only then is `X-Forwarded-Proto` honored and the `Secure`
  cookie flag added). Never expose directly to the public internet.

## Hardening history

| Item | State |
|---|---|
| Body size limit / robust decoding / error text | Fixed v1.0.1 (413/400/generic 500) |
| Field-level validation + path traversal (`withinDir` + realpath, P0-01) | Fixed v1.0.2 / v1.0.5 |
| `AUTH_TOKEN` Bearer gate for write APIs | Superseded v1.0.7 by full session gate |
| Unified session auth, CSRF, anonymous-path method binding | **v1.0.7** |
| Host allow-list (DNS-rebinding) | **v1.0.7** |
| Security regression suite R1–R22 | v1.0.7: 164 assertions green locally and in CI (one symlink case skips on Windows runners) |

## Known limitations (honest list)

- With `TRUST_PROXY=1`, the login rate-limiter keys on the shared proxy IP.
- In-memory sessions ⇒ single process; rolling restarts log users out.
- `url.parse()` triggers a Node deprecation warning (DEP0169); do not migrate to WHATWG URL
  without first porting the 12 path-variant regression cases (see the route matrix doc §3).
- `/data` must never be served statically; it is outside the public root by construction.

The full internal route inventory and gap audit (G1–G12) is tracked in private project docs;
a public summary matrix lives in [docs/ROUTE-SECURITY-MATRIX.md](ROUTE-SECURITY-MATRIX.md).
