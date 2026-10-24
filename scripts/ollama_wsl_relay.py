#!/usr/bin/env python3
"""WSL -> Windows Ollama relay.

Ollama on Windows listens on Windows-localhost (127.0.0.1:11434), which a WSL2
NAT-mode distro cannot reach, and opening the Windows firewall requires admin
rights. This relay listens on WSL's 127.0.0.1 and forwards each HTTP request
through ``curl.exe`` (a Windows process launched via WSL interop), which *can*
reach Windows-localhost.

* Bound to loopback only; Ollama is never exposed to the LAN.
* Request bodies (which may contain patient text) travel over the child
  process's stdin, never on a command line or in logs.
* ``curl --raw -i`` returns the exact upstream bytes, so streaming responses
  (chunked NDJSON) pass through unchanged.

Not needed when WSL uses ``networkingMode=mirrored`` or when Ollama runs in
the same environment as the API; then point MEDASSIST_OLLAMA_URL at it
directly.

Usage: python3 scripts/ollama_wsl_relay.py [--listen 127.0.0.1:11434]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil

log = logging.getLogger("ollama-relay")

MAX_BODY = 32 * 1024 * 1024
FORWARD_HEADERS = {"content-type", "accept"}
ALLOWED_PATH_PREFIXES = ("/api/", "/v1/")


def find_curl_exe() -> str:
    path = shutil.which("curl.exe") or "/mnt/c/Windows/System32/curl.exe"
    return path


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, upstream: str, curl: str) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    except Exception:
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, path, _ = lines[0].split(" ", 2)
    except ValueError:
        writer.close()
        return
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    if method not in {"GET", "POST", "HEAD", "DELETE"} or not path.startswith(ALLOWED_PATH_PREFIXES):
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return

    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_BODY:
        writer.write(b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    body = await reader.readexactly(length) if length else b""

    args = [curl, "-sS", "--raw", "-i", "-N", "--max-time", "900", "-X", method, "-H", "Connection: close"]
    for k in FORWARD_HEADERS:
        if k in headers:
            args += ["-H", f"{k}: {headers[k]}"]
    if body:
        args += ["--data-binary", "@-"]
    args.append(upstream + path)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdin and proc.stdout
    if body:
        proc.stdin.write(body)
    proc.stdin.close()

    # If the client goes away (timeout/cancel), kill curl.exe so Ollama stops
    # generating; otherwise abandoned generations queue ahead of new requests.
    async def watch_disconnect() -> None:
        try:
            more = await reader.read(1)
        except Exception:  # noqa: BLE001
            more = b""
        if not more and proc.returncode is None:
            proc.kill()

    watcher = asyncio.create_task(watch_disconnect())

    # curl -i may emit interim "HTTP/1.1 100 Continue" blocks; drop them.
    buf = b""
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            if buf is not None:
                buf += chunk
                while buf.startswith(b"HTTP/1.1 100") and b"\r\n\r\n" in buf:
                    buf = buf.split(b"\r\n\r\n", 1)[1]
                if buf.startswith(b"HTTP/") and b"\r\n\r\n" in buf:
                    status, rest = buf.split(b"\r\n", 1)
                    # Ensure the client closes after this response.
                    writer.write(status + b"\r\nConnection: close\r\n" + _strip_conn(rest))
                    buf = None
                continue
            writer.write(chunk)
            await writer.drain()
        if buf:
            writer.write(buf)
        rc = await proc.wait()
        if rc != 0 and buf is not None and not buf:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        if proc.returncode is None:
            proc.kill()
    finally:
        watcher.cancel()
        writer.close()


def _strip_conn(rest: bytes) -> bytes:
    head, sep, body = rest.partition(b"\r\n\r\n")
    kept = [h for h in head.split(b"\r\n") if not h.lower().startswith(b"connection:")]
    return b"\r\n".join(kept) + sep + body


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:11434")
    ap.add_argument("--upstream", default="http://127.0.0.1:11434", help="URL as seen from Windows")
    a = ap.parse_args()
    host, port = a.listen.rsplit(":", 1)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing to bind the relay to a non-loopback address.")
    curl = find_curl_exe()
    server = await asyncio.start_server(lambda r, w: handle(r, w, a.upstream, curl), host, int(port))
    log.warning("relay listening on %s -> %s (via %s)", a.listen, a.upstream, curl)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main())
