# Security

Status: **hardening in progress (ROADMAP)**. The server is a localhost-first tool; it is **not**
designed to be exposed to the public internet without additional protection.

## Threat model

- Default bind is `127.0.0.1`; the LAN mode (`平板访问.bat` sets `HOST=0.0.0.0`) intentionally
  exposes the reader and progress/bookmark/practice write APIs to the local network.
- The server stores user data (`data/progress.json`, `data/bookmarks.json`, `data/practice.json`)
  and serves book content only from the `books/` directory.

## Hardening items (priority order)

Status update 2026-11-25 (v1.0.2): **P0 + P1 complete; P2 shipped** (opt-in `AUTH_TOKEN`
gate for write APIs + `tools/test_server_security.js` wired into CI, 26 cases green locally
and in a repo-shaped layout).

| Priority | Item | State |
|---|---|---|
| P0 | Request body size limit (memory DoS on `/api/*` writes) | **Fixed v1.0.1** — 256 KB cap, 413 |
| P0 | Robust `decodeURIComponent` + top-level error handling | **Fixed v1.0.1** — 400 on illegal encoding |
| P0 | Don't leak internal error messages to clients | **Fixed v1.0.1** — generic 500 text |
| P1 | Field-level validation (bookId/chapter/page length & range) | **Fixed v1.0.2** — ids ≤120 & no separators/NUL, page numeric+bounded, chapter ≤200; invalid input never reaches disk |
| P1 | Path traversal defense (asset sub-path + URL id + NUL) | **Fixed v1.0.2** — normalize + prefix + length checks + NUL rejection |
| P2 | Lite auth for write APIs | **v1.0.2** — opt-in `AUTH_TOKEN` env; when set, non-GET `/api/*` requires `Authorization: Bearer`. Frontend does not send it yet: enabling in LAN mode blocks client writes with 401 (documented tradeoff) |
| P2 | Security regression tests | **Fixed v1.0.2** — `node tools/test_server_security.js` in CI (413/400/403/401 + normal-flow no-regression), runs in isolated `BOOKROOT` temp dir |

`server.js` also gained a `BOOKROOT` env override (same convention as `tools/paths.py`):
set it to serve library data from another directory; defaults to the repo root.

## Principles

- Books data never enters public repos; `.gitignore` covers `books/`, `题库/`, `_dist/`, PDFs.
- Path safety rule: `basename` + explicit prefix check before any `fs` read/write.
- Atomic writes: every JSON write goes through `tmp` + `rename`.
- If you must expose the server beyond localhost/LAN, add a reverse proxy with authentication
  and rate limiting; do not run it directly on the internet.

Details and the full remediation plan live in the private project docs (`安全修复方案.md`).