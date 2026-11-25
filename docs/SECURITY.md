# Security

Status: **hardening in progress (ROADMAP)**. The server is a localhost-first tool; it is **not**
designed to be exposed to the public internet without additional protection.

## Threat model

- Default bind is `127.0.0.1`; the LAN mode (`平板访问.bat` sets `HOST=0.0.0.0`) intentionally
  exposes the reader and progress/bookmark/practice write APIs to the local network.
- The server stores user data (`data/progress.json`, `data/bookmarks.json`, `data/practice.json`)
  and serves book content only from the `books/` directory.

## Hardening items (priority order)

Status update 2026-11-25: **all P0 items shipped in v1.0.1**; P1 asset-path normalize shipped,
field validation pending; P2 items still open.

| Priority | Item | State |
|---|---|---|
| P0 | Request body size limit (memory DoS on `/api/*` writes) | **Fixed v1.0.1** — 256 KB cap, 413 |
| P0 | Robust `decodeURIComponent` + top-level error handling | **Fixed v1.0.1** — 400 on illegal encoding |
| P0 | Don't leak internal error messages to clients | **Fixed v1.0.1** — generic 500 text |
| P1 | Field-level validation (bookId/chapter/page length & range) | Open |
| P1 | Path traversal defense tightening (asset sub-path normalize) | **Partial v1.0.1** — normalize + prefix check |
| P2 | Lite auth for write APIs when bound to LAN | Open |
| P2 | Security regression tests (413/400/403 cases) | Smoke-verified manually; in-repo test open |

## Principles

- Books data never enters public repos; `.gitignore` covers `books/`, `题库/`, `_dist/`, PDFs.
- Path safety rule: `basename` + explicit prefix check before any `fs` read/write.
- Atomic writes: every JSON write goes through `tmp` + `rename`.
- If you must expose the server beyond localhost/LAN, add a reverse proxy with authentication
  and rate limiting; do not run it directly on the internet.

Details and the full remediation plan live in the private project docs (`安全修复方案.md`).