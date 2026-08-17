#!/usr/bin/env python3
"""Minimal storage service for immutable Codex session packages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
import re
import secrets
import ssl
import tempfile
import threading
import time
import tarfile
from urllib.parse import unquote, urlsplit


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
CHUNK_SIZE = 1024 * 1024
MAX_AUTH_BODY_BYTES = 8 * 1024
SESSION_SECONDS = 12 * 60 * 60


class AuthStore:
    """Small, file-backed account verifier with in-memory HTTPS sessions."""

    def __init__(self, accounts_file: Path) -> None:
        try:
            document = json.loads(accounts_file.read_text(encoding="utf-8"))
            records = document["accounts"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"could not read accounts file {accounts_file}: {exc}") from exc
        if not isinstance(records, dict) or not records:
            raise ValueError("accounts file must contain at least one account")
        self.records: dict[str, dict[str, object]] = {}
        for username, record in records.items():
            if not isinstance(username, str) or not IDENTIFIER.fullmatch(username) or not isinstance(record, dict):
                raise ValueError("accounts file contains an invalid account")
            try:
                salt = bytes.fromhex(str(record["salt_hex"]))
                password_hash = bytes.fromhex(str(record["password_hash_hex"]))
                iterations = int(record["iterations"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"account {username!r} has an invalid password record") from exc
            if len(salt) < 16 or len(password_hash) != 32 or iterations < 100_000:
                raise ValueError(f"account {username!r} has an unsafe password record")
            self.records[username] = {"salt": salt, "password_hash": password_hash, "iterations": iterations}
        self.sessions: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def verify_password(self, username: str, password: str) -> bool:
        record = self.records.get(username)
        if record is None:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), record["salt"], record["iterations"]
        )
        return secrets.compare_digest(candidate, record["password_hash"])

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[token] = (username, time.time() + SESSION_SECONDS)
        return token

    def account_for_session(self, token: str | None) -> str | None:
        if not token:
            return None
        with self.lock:
            details = self.sessions.get(token)
            if details is None:
                return None
            username, expires_at = details
            if expires_at <= time.time():
                self.sessions.pop(token, None)
                return None
            return username

    def delete_session(self, token: str | None) -> None:
        if token:
            with self.lock:
                self.sessions.pop(token, None)


class PackageHandler(BaseHTTPRequestHandler):
    storage_root: Path
    maximum_bytes: int
    web_root: Path
    auth_store: AuthStore

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}")

    def reply_json(self, status: HTTPStatus, body: dict, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def serve_index(self) -> None:
        index = self.web_root / "index.html"
        content = index.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def read_json_body(self) -> dict | None:
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, ValueError):
            self.reply_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return None
        if length < 0 or length > MAX_AUTH_BODY_BYTES:
            self.reply_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body is too large"})
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.reply_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return None
        if not isinstance(body, dict):
            self.reply_json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
            return None
        return body

    def session_token(self) -> str | None:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie"))
            value = cookie.get("codex_sync_session")
            return value.value if value else None
        except (ValueError, KeyError):
            return None

    def basic_account(self) -> str | None:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Basic "):
            return None
        try:
            username, password = base64.b64decode(value[6:], validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None
        return username if self.auth_store.verify_password(username, password) else None

    def authenticated_account(self) -> str | None:
        account = self.auth_store.account_for_session(self.session_token()) or self.basic_account()
        if account is None:
            self.reply_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "authentication required"},
                {"WWW-Authenticate": 'Basic realm="codex-sync"'},
            )
        return account

    def serve_static(self, path: str) -> bool:
        files = {
            "/static/sql-wasm.js": ("vendor/sql-wasm.js", "application/javascript; charset=utf-8"),
            "/static/sql-wasm.wasm": ("vendor/sql-wasm.wasm", "application/wasm"),
        }
        entry = files.get(path)
        if entry is None:
            return False
        content = (self.web_root / entry[0]).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", entry[1])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(content)
        return True

    def package_path(self) -> Path | None:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 7 or parts[0] != "v1" or parts[1] != "accounts" or parts[3] != "tags" or parts[5] != "packages":
            return None
        account, tag, package_id = parts[2], parts[4], parts[6]
        if not all(IDENTIFIER.fullmatch(value) for value in (account, tag, package_id)):
            return None
        return self.storage_root / "accounts" / account / "tags" / tag / "packages" / package_id

    def inventory_account(self) -> str | None:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 4 or parts[:2] != ["v1", "accounts"] or parts[3] != "packages":
            return None
        return parts[2] if IDENTIFIER.fullmatch(parts[2]) else None

    def package_summary(self, package: Path, tag: str) -> dict:
        summary = {
            "package_id": package.name,
            "tag": tag,
            "bytes": package.stat().st_size,
            "modified_at": int(package.stat().st_mtime),
            "valid": False,
        }
        try:
            with tarfile.open(package, "r:gz") as bundle:
                stream = bundle.extractfile(".codex-sync/manifest.json")
                if stream is None:
                    return summary
                manifest = json.load(stream)
            registration = manifest.get("thread_registration", {})
            record = registration.get("record", {})
            entry = manifest.get("entries", [{}])[0]
            if not isinstance(record, dict) or not isinstance(entry, dict):
                return summary
            title = record.get("title") or record.get("first_user_message") or package.name
            summary.update(
                {
                    "valid": True,
                    "thread_id": record.get("id"),
                    "title": title if isinstance(title, str) else package.name,
                    "updated_at": record.get("updated_at"),
                    "session_path": entry.get("path"),
                }
            )
        except (OSError, tarfile.TarError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return summary

    def reply_inventory(self, account: str) -> None:
        root = self.storage_root / "accounts" / account / "tags"
        packages: list[dict] = []
        if root.is_dir():
            for tag_directory in root.iterdir():
                package_directory = tag_directory / "packages"
                if not IDENTIFIER.fullmatch(tag_directory.name) or not package_directory.is_dir():
                    continue
                for package in package_directory.iterdir():
                    if package.is_file() and IDENTIFIER.fullmatch(package.name):
                        packages.append(self.package_summary(package, tag_directory.name))
        packages.sort(key=lambda item: item["modified_at"], reverse=True)
        self.reply_json(HTTPStatus.OK, {"account": account, "packages": packages})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.serve_index()
            return
        if self.serve_static(path):
            return
        if path == "/healthz":
            self.reply_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/v1/auth/me":
            account = self.authenticated_account()
            if account is not None:
                self.reply_json(HTTPStatus.OK, {"account": account})
            return
        inventory_account = self.inventory_account()
        if inventory_account is not None:
            account = self.authenticated_account()
            if account is None:
                return
            if account != inventory_account:
                self.reply_json(HTTPStatus.FORBIDDEN, {"error": "account does not match authenticated user"})
                return
            self.reply_inventory(account)
            return
        package = self.package_path()
        if package is None:
            self.reply_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        account = self.authenticated_account()
        if account is None:
            return
        if account != package.parents[3].name:
            self.reply_json(HTTPStatus.FORBIDDEN, {"error": "account does not match authenticated user"})
            return
        if not package.is_file():
            self.reply_json(HTTPStatus.NOT_FOUND, {"error": "package not found"})
            return
        digest = hashlib.sha256()
        with package.open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(block)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(package.stat().st_size))
        self.send_header("X-Content-SHA256", digest.hexdigest())
        self.end_headers()
        with package.open("rb") as stream:
            while block := stream.read(CHUNK_SIZE):
                self.wfile.write(block)

    def do_PUT(self) -> None:
        package = self.package_path()
        if package is None:
            self.reply_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path or hash"})
            return
        account = self.authenticated_account()
        if account is None:
            return
        if account != package.parents[3].name:
            self.reply_json(HTTPStatus.FORBIDDEN, {"error": "account does not match authenticated user"})
            return
        expected_hash = self.headers.get("X-Content-SHA256", "")
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, ValueError):
            self.reply_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            self.reply_json(HTTPStatus.BAD_REQUEST, {"error": "invalid path or hash"})
            return
        if length < 0 or length > self.maximum_bytes:
            self.reply_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "package exceeds configured limit"})
            return
        package.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=package.parent, delete=False) as temp:
            temporary_path = Path(temp.name)
            remaining = length
            while remaining:
                block = self.rfile.read(min(CHUNK_SIZE, remaining))
                if not block:
                    temporary_path.unlink(missing_ok=True)
                    self.reply_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete request body"})
                    return
                remaining -= len(block)
                digest.update(block)
                temp.write(block)
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            temporary_path.unlink(missing_ok=True)
            self.reply_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "content hash mismatch"})
            return
        if package.exists():
            if hashlib.sha256(package.read_bytes()).hexdigest() == actual_hash:
                temporary_path.unlink(missing_ok=True)
                self.reply_json(HTTPStatus.OK, {"status": "already-present", "sha256": actual_hash})
                return
            temporary_path.unlink(missing_ok=True)
            self.reply_json(HTTPStatus.CONFLICT, {"error": "package id already exists with different bytes"})
            return
        temporary_path.replace(package)
        self.reply_json(HTTPStatus.CREATED, {"status": "stored", "sha256": actual_hash, "bytes": length})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/v1/auth/login":
            body = self.read_json_body()
            if body is None:
                return
            username, password = body.get("username"), body.get("password")
            if not isinstance(username, str) or not isinstance(password, str) or not self.auth_store.verify_password(username, password):
                self.reply_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid username or password"})
                return
            token = self.auth_store.create_session(username)
            cookie = f"codex_sync_session={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
            self.reply_json(HTTPStatus.OK, {"account": username}, {"Set-Cookie": cookie})
            return
        if path == "/v1/auth/logout":
            self.auth_store.delete_session(self.session_token())
            self.reply_json(
                HTTPStatus.OK,
                {"status": "logged-out"},
                {"Set-Cookie": "codex_sync_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"},
            )
            return
        self.reply_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex-sync proof-of-concept storage service")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--root", required=True, help="directory containing account/tag package folders")
    parser.add_argument("--max-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--certfile", help="PEM TLS certificate; required with --keyfile")
    parser.add_argument("--keyfile", help="PEM TLS private key; required with --certfile")
    parser.add_argument("--accounts-file", required=True, help="JSON account records with PBKDF2 password hashes")
    args = parser.parse_args()
    PackageHandler.storage_root = Path(args.root).resolve()
    PackageHandler.maximum_bytes = args.max_bytes
    PackageHandler.web_root = Path(__file__).resolve().parent / "web"
    try:
        PackageHandler.auth_store = AuthStore(Path(args.accounts_file))
    except ValueError as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer((args.bind, args.port), PackageHandler)
    protocol = "http"
    if bool(args.certfile) != bool(args.keyfile):
        parser.error("--certfile and --keyfile must be supplied together")
    if args.certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.certfile, args.keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        protocol = "https"
    print(f"listening on {protocol}://{args.bind}:{args.port}; storage={PackageHandler.storage_root}")
    server.serve_forever()


if __name__ == "__main__":
    main()
