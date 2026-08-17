# codex-sync proof of concept

This repository contains a deliberately narrow verification tool: package one
Codex session JSONL unchanged together with its `threads` registry row, upload
it to a service, download it, verify its hash, and restore it below a selected
`CODEX_HOME`.

It does not parse conversation content. On restore it inserts the saved
registration row into `state_5.sqlite` and rewrites only `rollout_path` for the
target machine. It creates a SQLite backup before any registration write.

## Commands

The CLI stores only a server session token in `~/.codex-sync/sessions.json`
with mode `0600`; it never saves the password.

```sh
# The current proof server uses a self-signed HTTPS certificate.
python3 codex_sync.py login --insecure --url https://sync.example.com --account demo

python3 codex_sync.py list --url https://sync.example.com --account demo --tag mac

python3 codex_sync.py push --url https://sync.example.com --account demo --tag mac \
  --session ~/.codex/sessions/YYYY/MM/DD/rollout-...jsonl

# Download only: this does not touch local Codex state.
python3 codex_sync.py download --url https://sync.example.com --account demo --tag mac \
  --package-id <package-id> --output ~/Downloads/thread.tar.gz

# Load an existing local package into Codex.
python3 codex_sync.py load --package ~/Downloads/thread.tar.gz --codex-home ~/.codex

# Download and load in one command.
python3 codex_sync.py pull --url https://sync.example.com --account demo --tag mac \
  --package-id <package-id> --codex-home ~/.codex

python3 codex_sync.py logout --url https://sync.example.com --account demo
```

`push` creates no permanent local package. `download` only writes the requested
package file. `load` restores an existing local package and registers its
thread. `pull` is the one-step download-and-load operation. The low-level
`pack`, `upload`, and `restore` aliases remain available for diagnostics.

For a temporary self-signed HTTPS certificate, add `--insecure` to `login`.
The choice is saved with that local session profile. `CODEX_SYNC_PASSWORD`
remains available as a non-persistent fallback for automation.

## Browser page

The service also serves a browser page at `/`. It has no registration flow:
the operator creates accounts in the server-only account file. After logging
in, the user explicitly selects their local `.codex` directory. The page reads
`state_5.sqlite` in the browser to show thread titles and timestamps, and can
package/upload a selected JSONL without parsing its conversation content. The
cloud repository lists packages by tag and downloads raw packages. It does not
load packages into `.codex`; instead it generates a copyable `pull` command
after the user enters a target directory.

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
