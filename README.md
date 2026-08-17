# codex-sync proof of concept

This repository contains a deliberately narrow verification tool: package one
Codex session JSONL unchanged together with its `threads` registry row, upload
it to a service, download it, verify its hash, and restore it below a selected
`CODEX_HOME`.

It does not parse conversation content. On restore it inserts the saved
registration row into `state_5.sqlite` and rewrites only `rollout_path` for the
target machine. It creates a SQLite backup before any registration write.

## Commands

```sh
export CODEX_SYNC_PASSWORD='your-password'

python3 codex_sync.py push --url https://sync.example.com --account demo --tag mac \
  --session ~/.codex/sessions/YYYY/MM/DD/rollout-...jsonl
python3 codex_sync.py pull --url https://sync.example.com --account demo --tag mac \
  --package-id <printed-package-id> --codex-home /tmp/restore-home
```

`push` creates no permanent local package. `pull` downloads, restores, and
registers the thread in one command. The low-level `pack`, `upload`,
`download`, and `restore` commands remain available for diagnostics.

The client reads the password from `CODEX_SYNC_PASSWORD`, so the password does
not appear in a command argument. `--username` defaults to `--account`.
For a temporary self-signed HTTPS certificate, add `--insecure`; do not use it
once the service has a trusted certificate.

## Browser page

The service also serves a browser page at `/`. It has no registration flow:
the operator creates accounts in the server-only account file. After logging
in, the user explicitly selects their local `.codex` directory. The page reads
only `state_5.sqlite` in the browser to show thread titles and timestamps; it
does not read or upload conversation JSONL at that point.

`sql.js` is vendored under `web/vendor/` so title listing does not depend on a
third-party CDN at runtime.

## Service setup

The service requires a private JSON account file. It contains PBKDF2-HMAC-SHA256
records and must not be committed:

```sh
install -d -m 700 /etc/codex-sync
chmod 600 /etc/codex-sync/accounts.json
```

Each record uses this shape:

```json
{
  "accounts": {
    "demo": {
      "salt_hex": "at-least-16-random-bytes-in-hex",
      "password_hash_hex": "pbkdf2-sha256-result-in-hex",
      "iterations": 600000
    }
  }
}
```

Run the HTTPS service with:

```sh
python3 codex_sync_server.py --root /var/lib/codex-sync --bind 0.0.0.0 --port 25563 \
  --certfile /etc/codex-sync/tls.crt --keyfile /etc/codex-sync/tls.key \
  --accounts-file /etc/codex-sync/accounts.json
```
