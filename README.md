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
python3 codex_sync.py push --url http://127.0.0.1:8089 --account demo --tag mac \
  --session ~/.codex/sessions/YYYY/MM/DD/rollout-...jsonl
python3 codex_sync.py pull --url http://127.0.0.1:8089 --account demo --tag mac \
  --package-id <printed-package-id> --codex-home /tmp/restore-home
```

`push` creates no permanent local package. `pull` downloads, restores, and
registers the thread in one command. The low-level `pack`, `upload`,
`download`, and `restore` commands remain available for diagnostics.

The verification service is intentionally bound to the server loopback
interface until account authentication exists. Reach it through SSH:

```sh
ssh -N -L 18090:127.0.0.1:25563 root@43.136.115.91
```

Then use `--url http://127.0.0.1:18090` in the commands above.

Run the service with:

```sh
python3 codex_sync_server.py --root /var/lib/codex-sync --bind 127.0.0.1 --port 8089
```
