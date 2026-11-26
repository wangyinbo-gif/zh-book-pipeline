# Route Security Matrix (public summary, 2026-11-26)

Companion to [SECURITY.md](SECURITY.md). The authoritative internal matrix (37 routes, 12
path-variant probes) is kept in private project docs; this file records the classification
contract that code and tests enforce.

| Route class | Methods | Access class | Notes |
|---|---|---|---|
| `/login.html` | GET HEAD | anonymous (method-bound 405) | no external assets; self-contained |
| `/api/auth/login`, `/api/auth/logout` | POST | anonymous (method-bound) | rate limited 10/min per IP |
| `/api/auth/status` | GET HEAD | anonymous | `{authed, mode, csrf?}` only |
| `/api/health` | GET HEAD | anonymous | minimal `{ok, mode}`, no counters |
| `/api/*` business endpoints | GET (reads) / POST PUT PATCH DELETE (writes) | session | writes also require CSRF token + same-origin |
| `/books/<hash>/assets/*`, `/data/*` static | GET HEAD only (else 405) | session | traversal guard (prefix + realpath) unchanged |
| Any other path | — | default deny (401 API-like, 302 navigational) | leading-slash variants normalized before classification |

Enforcement order: Host allow-list → anonymous method binding → open-mode passthrough →
authentication → CSRF → dispatch. Bypass classes verified negative: case tricks, single/double
percent-encoding, extra slashes, dot segments, trailing dots/spaces, 8.3 names, NTFS alternate
streams, symlinks (all 401/302/400 in session mode; static resolution unchanged in open mode).
The suite `tools/test_server_security.js` (R1–R22, 164 assertions) locks every row above.
