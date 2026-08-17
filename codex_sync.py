#!/usr/bin/env python3
"""File-level packaging and transfer for Codex session archives.

This intentionally never parses a conversation JSONL file.  It only preserves
one session archive as bytes under its original path below CODEX_HOME.
"""

from __future__ import annotations

import argparse
import base64
from http.cookies import SimpleCookie
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
import tarfile
import tempfile
import time
from urllib import error, request


FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SESSION_ID = re.compile(r"([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\.jsonl\Z")
AUTH_FILE = Path.home() / ".codex-sync" / "sessions.json"


class SyncError(Exception):
    """An expected command-line error."""


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def session_relative_path(session: Path, codex_home: Path) -> Path:
    resolved_session = session.resolve(strict=True)
    sessions_root = (codex_home.resolve(strict=True) / "sessions").resolve(strict=True)
    try:
        relative = resolved_session.relative_to(codex_home.resolve(strict=True))
    except ValueError as exc:
        raise SyncError("session must be located below CODEX_HOME") from exc
    if resolved_session.parent != sessions_root and sessions_root not in resolved_session.parents:
        raise SyncError("session must be located below CODEX_HOME/sessions")
    if not relative.name.endswith(".jsonl"):
        raise SyncError("session must be a .jsonl file")
    return relative


def thread_id_from_session(session: Path) -> str:
    match = SESSION_ID.search(session.name)
    if match is None:
        raise SyncError("session filename does not end in a UUID")
    return match.group(1)


def sqlite_read_connection(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise SyncError(f"missing Codex thread registry: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def source_thread_record(codex_home: Path, session: Path) -> dict:
    thread_id = thread_id_from_session(session)
    database = codex_home / "state_5.sqlite"
    try:
        with sqlite_read_connection(database) as connection:
            row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    except sqlite3.Error as exc:
        raise SyncError(f"could not read Codex thread registry: {exc}") from exc
    if row is None:
        raise SyncError(f"no threads registry row exists for session {thread_id}")
    return dict(row)


def quoted_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise SyncError(f"unsafe database identifier: {name}")
    return f'"{name}"'


def load_manifest(archive: Path) -> dict:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember(".codex-sync/manifest.json")
            stream = bundle.extractfile(member)
            if stream is None:
                raise SyncError("package manifest is unreadable")
            manifest = json.load(stream)
    except (OSError, tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        raise SyncError(f"invalid package: {exc}") from exc
    if manifest.get("format") != FORMAT_VERSION or not isinstance(manifest.get("entries"), list):
        raise SyncError("unsupported package manifest")
    return manifest


def pack(args: argparse.Namespace) -> None:
    session = Path(args.session).expanduser()
    codex_home = Path(args.codex_home).expanduser()
    relative = session_relative_path(session, codex_home)
    record = source_thread_record(codex_home, session)
    if record["id"] != thread_id_from_session(session):
        raise SyncError("thread registry UUID did not match session filename")
    destination = Path(args.output).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_path(session)
    manifest = {
        "format": FORMAT_VERSION,
        "entries": [{"path": relative.as_posix(), "sha256": source_hash, "size": session.stat().st_size}],
        "thread_registration": {"table": "threads", "record": record},
    }
    with tarfile.open(destination, "w:gz") as bundle:
        manifest_data = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        manifest_info = tarfile.TarInfo(".codex-sync/manifest.json")
        manifest_info.size = len(manifest_data)
        bundle.addfile(manifest_info, io.BytesIO(manifest_data))
        bundle.add(session, arcname=relative.as_posix(), recursive=False)
    print(
        json.dumps(
            {
                "package": str(destination),
                "sha256": sha256_path(destination),
                "format": FORMAT_VERSION,
                "session_path": relative.as_posix(),
                "session_sha256": source_hash,
                "thread_id": record["id"],
                "thread_registration_included": True,
            },
            ensure_ascii=False,
        )
    )


def url_for(args: argparse.Namespace) -> str:
    for value, label in ((args.account, "account"), (args.tag, "tag"), (args.package_id, "package id")):
        if not IDENTIFIER.fullmatch(value):
            raise SyncError(f"invalid {label}; use letters, digits, '.', '_' or '-'")
    return f"{args.url.rstrip('/')}/v1/accounts/{args.account}/tags/{args.tag}/packages/{args.package_id}"


def auth_key(url: str, account: str) -> str:
    return f"{url.rstrip('/')}\n{account}"


def read_auth_records() -> dict:
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"could not read local login state: {exc}") from exc


def write_auth_records(records: dict) -> None:
    AUTH_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=AUTH_FILE.parent, mode="w", encoding="utf-8", delete=False) as temporary:
        temporary.write(json.dumps(records, sort_keys=True))
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, AUTH_FILE)


def authorization_header(args: argparse.Namespace) -> dict[str, str]:
    record = read_auth_records().get(auth_key(args.url, args.account))
    if isinstance(record, dict) and isinstance(record.get("session_token"), str):
        return {"Cookie": f"codex_sync_session={record['session_token']}"}
    password = os.environ.get(args.password_env)
    if password is None:
        raise SyncError(f"run 'codex-sync login --url {args.url} --account {args.account}' first")
    username = args.username or args.account
    credential = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {credential}"}


def open_request(req: request.Request, args: argparse.Namespace):
    record = read_auth_records().get(auth_key(args.url, args.account))
    allow_self_signed = args.insecure or (isinstance(record, dict) and record.get("insecure") is True)
    context = ssl._create_unverified_context() if allow_self_signed else None
    return request.urlopen(req, timeout=args.timeout, context=context)


def upload(args: argparse.Namespace) -> None:
    package = Path(args.package).expanduser()
    package_hash = sha256_path(package)
    headers = {
        "Content-Type": "application/gzip",
        "Content-Length": str(package.stat().st_size),
        "X-Content-SHA256": package_hash,
    }
    headers.update(authorization_header(args))
    req = request.Request(
        url_for(args),
        data=package.read_bytes(),
        method="PUT",
        headers=headers,
    )
    try:
        with open_request(req, args) as response:
            print(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise SyncError(f"upload failed: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except error.URLError as exc:
        raise SyncError(f"upload failed: {exc.reason}") from exc


def download(args: argparse.Namespace) -> None:
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    req = request.Request(url_for(args), method="GET", headers=authorization_header(args))
    try:
        with open_request(req, args) as response:
            expected_hash = response.headers.get("X-Content-SHA256")
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temp:
                temporary_path = Path(temp.name)
                digest = hashlib.sha256()
                while block := response.read(CHUNK_SIZE):
                    temp.write(block)
                    digest.update(block)
        received_hash = digest.hexdigest()
        if not expected_hash or received_hash != expected_hash:
            temporary_path.unlink(missing_ok=True)
            raise SyncError("download hash did not match server response")
        os.replace(temporary_path, output)
        print(json.dumps({"package": str(output), "sha256": received_hash}, ensure_ascii=False))
    except error.HTTPError as exc:
        raise SyncError(f"download failed: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except error.URLError as exc:
        raise SyncError(f"download failed: {exc.reason}") from exc


def login(args: argparse.Namespace) -> None:
    password = getpass.getpass(f"Password for {args.account}: ")
    payload = json.dumps({"username": args.account, "password": password}).encode("utf-8")
    req = request.Request(
        f"{args.url.rstrip('/')}/v1/auth/login",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    context = ssl._create_unverified_context() if args.insecure else None
    try:
        with request.urlopen(req, timeout=args.timeout, context=context) as response:
            cookie = SimpleCookie(response.headers.get("Set-Cookie", ""))
            value = cookie.get("codex_sync_session")
            if value is None:
                raise SyncError("login response did not include a session token")
            records = read_auth_records()
            records[auth_key(args.url, args.account)] = {"session_token": value.value, "insecure": args.insecure}
            write_auth_records(records)
        print(json.dumps({"status": "logged-in", "account": args.account, "url": args.url.rstrip("/")}, ensure_ascii=False))
    except error.HTTPError as exc:
        raise SyncError(f"login failed: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except error.URLError as exc:
        raise SyncError(f"login failed: {exc.reason}") from exc


def logout(args: argparse.Namespace) -> None:
    records = read_auth_records()
    record = records.pop(auth_key(args.url, args.account), None)
    if isinstance(record, dict) and isinstance(record.get("session_token"), str):
        req = request.Request(
            f"{args.url.rstrip('/')}/v1/auth/logout",
            data=b"",
            method="POST",
            headers={"Cookie": f"codex_sync_session={record['session_token']}"},
        )
        context = ssl._create_unverified_context() if args.insecure or record.get("insecure") else None
        try:
            request.urlopen(req, timeout=args.timeout, context=context).close()
        except (error.HTTPError, error.URLError):
            pass
    write_auth_records(records)
    print(json.dumps({"status": "logged-out", "account": args.account, "url": args.url.rstrip("/")}, ensure_ascii=False))


def list_packages(args: argparse.Namespace) -> None:
    req = request.Request(
        f"{args.url.rstrip('/')}/v1/accounts/{args.account}/packages",
        method="GET",
        headers=authorization_header(args),
    )
    try:
        with open_request(req, args) as response:
            packages = json.loads(response.read())
    except error.HTTPError as exc:
        raise SyncError(f"list failed: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except error.URLError as exc:
        raise SyncError(f"list failed: {exc.reason}") from exc
    if args.tag:
        packages["packages"] = [package for package in packages.get("packages", []) if package.get("tag") == args.tag]
    print(json.dumps(packages, ensure_ascii=False, indent=2))


def push(args: argparse.Namespace) -> None:
    session = Path(args.session).expanduser()
    base_package_id = args.package_id or thread_id_from_session(session)
    with tempfile.TemporaryDirectory(prefix="codex-sync-push-") as temporary_directory:
        package = Path(temporary_directory) / "thread.tar.gz"
        pack(argparse.Namespace(session=str(session), output=str(package), codex_home=args.codex_home))
        package_id = args.package_id or f"{base_package_id}-{sha256_path(package)[:16]}"
        upload(
            argparse.Namespace(
                url=args.url,
                account=args.account,
                tag=args.tag,
                package_id=package_id,
                package=str(package),
                timeout=args.timeout,
                username=args.username,
                password_env=args.password_env,
                insecure=args.insecure,
            )
        )
    print(json.dumps({"status": "uploaded", "package_id": package_id}, ensure_ascii=False))


def pull(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="codex-sync-pull-") as temporary_directory:
        package = Path(temporary_directory) / "thread.tar.gz"
        download(
            argparse.Namespace(
                url=args.url,
                account=args.account,
                tag=args.tag,
                package_id=args.package_id,
                output=str(package),
                timeout=args.timeout,
                username=args.username,
                password_env=args.password_env,
                insecure=args.insecure,
            )
        )
        restore(
            argparse.Namespace(
                package=str(package),
                codex_home=args.codex_home,
                replace=args.replace,
                replace_registration=args.replace_registration,
            )
        )


def restore(args: argparse.Namespace) -> None:
    archive = Path(args.package).expanduser()
    codex_home = Path(args.codex_home).expanduser().resolve()
    manifest = load_manifest(archive)
    entries = manifest["entries"]
    if len(entries) != 1:
        raise SyncError("this proof-of-concept restores exactly one session per package")
    entry = entries[0]
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        raise SyncError("invalid package entry")
    relative = Path(entry["path"])
    if relative.is_absolute() or relative.parts[:1] != ("sessions",) or ".." in relative.parts:
        raise SyncError("unsafe session path in package")
    destination = (codex_home / relative).resolve()
    if codex_home not in destination.parents:
        raise SyncError("unsafe extraction target")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        allowed = {".codex-sync/manifest.json", relative.as_posix()}
        if {member.name for member in members} != allowed or any(not member.isfile() for member in members):
            raise SyncError("package contains unexpected archive members")
        source = bundle.extractfile(relative.as_posix())
        if source is None:
            raise SyncError("package session is unreadable")
        content = source.read()
    content_hash = hashlib.sha256(content).hexdigest()
    if content_hash != entry.get("sha256") or len(content) != entry.get("size"):
        raise SyncError("package session hash did not match manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_path(destination) == content_hash:
            file_status = "already-present"
        elif not args.replace:
            raise SyncError("destination exists with different bytes; rerun with --replace to overwrite")
        else:
            file_status = "restored"
    else:
        file_status = "restored"
    if file_status == "restored":
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.write(content)
        os.replace(temporary_path, destination)
    registration_status, backup = register_thread(codex_home, manifest, destination, args.replace_registration)
    result = {"status": file_status, "session": str(destination), "sha256": content_hash, "registration": registration_status}
    if backup is not None:
        result["registry_backup"] = str(backup)
    print(json.dumps(result, ensure_ascii=False))


def registration_record(manifest: dict, destination: Path) -> dict:
    registration = manifest.get("thread_registration")
    if not isinstance(registration, dict) or registration.get("table") != "threads":
        raise SyncError("package does not contain a threads registration record")
    record = registration.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
        raise SyncError("invalid threads registration record")
    if record["id"] != thread_id_from_session(destination):
        raise SyncError("registration UUID did not match restored session filename")
    copied = dict(record)
    copied["rollout_path"] = str(destination)
    return copied


def target_thread_columns(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute("PRAGMA table_info(threads)").fetchall()
    if not rows:
        raise SyncError("target state database has no threads table")
    return rows


def matching_thread_values(existing: sqlite3.Row, desired: dict, columns: list[str]) -> bool:
    return all(existing[column] == desired[column] for column in columns)


def backup_database(database: Path) -> Path:
    backup = database.with_name(f"{database.name}.codex-sync-backup-{time.time_ns()}")
    try:
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
    except sqlite3.Error as exc:
        backup.unlink(missing_ok=True)
        raise SyncError(f"could not back up Codex thread registry: {exc}") from exc
    return backup


def register_thread(codex_home: Path, manifest: dict, session_path: Path, replace: bool) -> tuple[str, Path | None]:
    database = codex_home / "state_5.sqlite"
    desired = registration_record(manifest, session_path)
    try:
        with sqlite_read_connection(database) as reader:
            schema = target_thread_columns(reader)
            target_columns = [row["name"] for row in schema]
            missing_required = [row["name"] for row in schema if row["notnull"] and row["dflt_value"] is None and row["name"] not in desired]
            if missing_required:
                raise SyncError(f"package is incompatible with target thread registry; missing {', '.join(missing_required)}")
            values = {name: desired[name] for name in target_columns if name in desired}
            existing = reader.execute("SELECT * FROM threads WHERE id = ?", (desired["id"],)).fetchone()
            if existing is not None and matching_thread_values(existing, values, list(values)):
                return "already-present", None
            if existing is not None and not replace:
                raise SyncError("thread registry already contains this UUID with different metadata; rerun with --replace-registration to overwrite it")
    except sqlite3.Error as exc:
        raise SyncError(f"could not inspect target thread registry: {exc}") from exc

    backup = backup_database(database)
    names = list(values)
    try:
        with sqlite3.connect(database, timeout=10) as writer:
            writer.row_factory = sqlite3.Row
            writer.execute("PRAGMA busy_timeout = 10000")
            writer.execute("BEGIN IMMEDIATE")
            existing = writer.execute("SELECT * FROM threads WHERE id = ?", (desired["id"],)).fetchone()
            if existing is not None and not replace:
                if matching_thread_values(existing, values, names):
                    writer.rollback()
                    return "already-present", None
                raise SyncError("thread registry changed during restore; rerun after closing Codex or use --replace-registration")
            if existing is None:
                columns_sql = ", ".join(quoted_identifier(name) for name in names)
                placeholders = ", ".join("?" for _ in names)
                writer.execute(f"INSERT INTO threads ({columns_sql}) VALUES ({placeholders})", [values[name] for name in names])
                status = "inserted"
            else:
                update_names = [name for name in names if name != "id"]
                assignments = ", ".join(f"{quoted_identifier(name)} = ?" for name in update_names)
                writer.execute(f"UPDATE threads SET {assignments} WHERE id = ?", [values[name] for name in update_names] + [desired["id"]])
                status = "replaced"
            writer.commit()
    except sqlite3.Error as exc:
        raise SyncError(f"could not register restored thread: {exc}") from exc
    return status, backup


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Lossless file-level Codex session sync proof of concept")
    commands = root.add_subparsers(dest="command", required=True)

    def add_connection(command: argparse.ArgumentParser) -> None:
        command.add_argument("--url", required=True)
        command.add_argument("--account", required=True)
        command.add_argument("--timeout", type=float, default=60)
        command.add_argument("--insecure", action="store_true", help="allow a self-signed HTTPS certificate")

    login_command = commands.add_parser("login", help="sign in and save a local session token")
    add_connection(login_command)
    login_command.set_defaults(func=login)

    logout_command = commands.add_parser("logout", help="remove the saved local session token")
    add_connection(logout_command)
    logout_command.set_defaults(func=logout)

    list_command = commands.add_parser("list", help="list packages in the cloud repository")
    add_connection(list_command)
    list_command.add_argument("--tag", help="optional tag filter")
    list_command.add_argument("--username", help=argparse.SUPPRESS)
    list_command.add_argument("--password-env", default="CODEX_SYNC_PASSWORD", help=argparse.SUPPRESS)
    list_command.set_defaults(func=list_packages)

    pack_command = commands.add_parser("pack", help="package one session JSONL and its threads registry row")
    pack_command.add_argument("--session", required=True)
    pack_command.add_argument("--output", required=True)
    pack_command.add_argument("--codex-home", default=str(default_codex_home()))
    pack_command.set_defaults(func=pack)

    def add_credentials(command: argparse.ArgumentParser) -> None:
        command.add_argument("--username", help="authenticated username; defaults to --account")
        command.add_argument("--password-env", default="CODEX_SYNC_PASSWORD", help="environment variable containing the password")

    for name, func, help_text in (("upload", upload, "upload a package"), ("download", download, "download a package")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--url", required=True)
        command.add_argument("--account", required=True)
        command.add_argument("--tag", required=True)
        command.add_argument("--package-id", required=True)
        command.add_argument("--timeout", type=float, default=60)
        command.add_argument("--insecure", action="store_true", help="allow a self-signed HTTPS certificate")
        add_credentials(command)
        if name == "upload":
            command.add_argument("--package", required=True)
        else:
            command.add_argument("--output", required=True)
        command.set_defaults(func=func)

    push_command = commands.add_parser("push", help="package and upload one Codex thread")
    push_command.add_argument("--url", required=True)
    push_command.add_argument("--account", required=True)
    push_command.add_argument("--tag", required=True)
    push_command.add_argument("--session", required=True)
    push_command.add_argument("--codex-home", default=str(default_codex_home()))
    push_command.add_argument("--package-id", help="optional immutable package ID; default includes UUID and content hash")
    push_command.add_argument("--timeout", type=float, default=60)
    push_command.add_argument("--insecure", action="store_true", help="allow a self-signed HTTPS certificate")
    add_credentials(push_command)
    push_command.set_defaults(func=push)

    pull_command = commands.add_parser("pull", help="download, restore, and register one Codex thread")
    pull_command.add_argument("--url", required=True)
    pull_command.add_argument("--account", required=True)
    pull_command.add_argument("--tag", required=True)
    pull_command.add_argument("--package-id", required=True)
    pull_command.add_argument("--codex-home", default=str(default_codex_home()))
    pull_command.add_argument("--timeout", type=float, default=60)
    pull_command.add_argument("--insecure", action="store_true", help="allow a self-signed HTTPS certificate")
    add_credentials(pull_command)
    pull_command.add_argument("--replace", action="store_true")
    pull_command.add_argument("--replace-registration", action="store_true")
    pull_command.set_defaults(func=pull)

    for name, help_text in (("load", "load one local package into Codex"), ("restore", "restore one package below CODEX_HOME/sessions")):
        restore_command = commands.add_parser(name, help=help_text)
        restore_command.add_argument("--package", required=True)
        restore_command.add_argument("--codex-home", default=str(default_codex_home()))
        restore_command.add_argument("--replace", action="store_true")
        restore_command.add_argument("--replace-registration", action="store_true")
        restore_command.set_defaults(func=restore)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
