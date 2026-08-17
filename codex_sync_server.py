#!/usr/bin/env python3
"""Minimal storage service for immutable Codex session packages."""

from __future__ import annotations

import argparse
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import ssl
import tempfile
from urllib.parse import unquote, urlsplit


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
CHUNK_SIZE = 1024 * 1024


class PackageHandler(BaseHTTPRequestHandler):
    storage_root: Path
    maximum_bytes: int
    web_root: Path

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}")

    def reply_json(self, status: HTTPStatus, body: dict) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
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

    def package_path(self) -> Path | None:
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 7 or parts[0] != "v1" or parts[1] != "accounts" or parts[3] != "tags" or parts[5] != "packages":
            return None
        account, tag, package_id = parts[2], parts[4], parts[6]
        if not all(IDENTIFIER.fullmatch(value) for value in (account, tag, package_id)):
            return None
        return self.storage_root / "accounts" / account / "tags" / tag / "packages" / package_id

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.serve_index()
            return
        if path == "/healthz":
            self.reply_json(HTTPStatus.OK, {"status": "ok"})
            return
        package = self.package_path()
        if package is None:
            self.reply_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
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
        expected_hash = self.headers.get("X-Content-SHA256", "")
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, ValueError):
            self.reply_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return
        if package is None or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex-sync proof-of-concept storage service")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--root", required=True, help="directory containing account/tag package folders")
    parser.add_argument("--max-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--certfile", help="PEM TLS certificate; required with --keyfile")
    parser.add_argument("--keyfile", help="PEM TLS private key; required with --certfile")
    args = parser.parse_args()
    PackageHandler.storage_root = Path(args.root).resolve()
    PackageHandler.maximum_bytes = args.max_bytes
    PackageHandler.web_root = Path(__file__).resolve().parent / "web"
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
