#!/usr/bin/env python3
"""
ocbox egress proxy: a tiny HTTPS CONNECT-only allowlist proxy (stdlib only).

It runs in its own container, attached to BOTH the internal network (where the
agent lives) and the egress network (which has internet). The agent container
has no other way out, so everything it sends to the internet passes through here.

Rules:
  * only the CONNECT method (HTTPS tunnels) on port 443; plain HTTP is refused
  * the target host must match the allowlist (unless MODE=learn)
  * the host must resolve to PUBLIC addresses only (no LAN / loopback / link-local)
  * every decision is logged to stdout as: ALLOW|DENY|LEARN <host>[ reason]

Env:  MODE=strict|learn   ALLOW_FILE=/proxy/allow.txt   PORT=3128
Allowlist entries: "example.com" (exact) or ".example.com" (domain + subdomains).
"""
import asyncio
import ipaddress
import os
import socket
import sys

MODE = os.environ.get("MODE", "strict")
PORT = int(os.environ.get("PORT", "3128"))
ALLOW_FILE = os.environ.get("ALLOW_FILE", "/proxy/allow.txt")


def load_allowlist(path):
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    entries.append(line)
    except FileNotFoundError:
        pass
    return entries


def host_allowed(host, entries):
    host = host.lower().rstrip(".")
    for e in entries:
        if e.startswith("."):
            if host == e[1:] or host.endswith(e):
                return True
        elif host == e:
            return True
    return False


def is_public(ip_text):
    ip = ipaddress.ip_address(ip_text)
    return ip.is_global  # False for private, loopback, link-local, multicast, ...


def log(msg):
    print(msg, flush=True)


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def reply(writer, status):
    writer.write(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode())
    try:
        await writer.drain()
    except ConnectionError:
        pass
    writer.close()


async def handle(reader, writer, entries):
    try:
        request_line = await asyncio.wait_for(reader.readline(), 10)
        # consume the remaining request headers
        while True:
            line = await asyncio.wait_for(reader.readline(), 10)
            if line in (b"\r\n", b"\n", b""):
                break
        parts = request_line.decode("latin-1").split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            log(f"DENY (non-CONNECT) {' '.join(parts[:2])}")
            return await reply(writer, "405 Method Not Allowed")

        host, _, port = parts[1].rpartition(":")
        host = host.strip("[]")
        if port != "443":
            log(f"DENY {host} port={port}")
            return await reply(writer, "403 Forbidden")

        if MODE == "learn":
            log(f"LEARN {host}")
        elif host_allowed(host, entries):
            log(f"ALLOW {host}")
        else:
            log(f"DENY {host}")
            return await reply(writer, "403 Forbidden")

        # Resolve ourselves and refuse non-public addresses.
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addrs = [i[4][0] for i in infos]
        public = [a for a in addrs if is_public(a)]
        if not public:
            log(f"DENY {host} resolves to non-public address(es): {addrs}")
            return await reply(writer, "403 Forbidden")

        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(public[0], 443), 15
        )
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    except Exception as exc:  # network errors, timeouts, odd input
        log(f"ERROR {type(exc).__name__}: {exc}")
        try:
            await reply(writer, "502 Bad Gateway")
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    entries = load_allowlist(ALLOW_FILE)
    log(f"proxy up: mode={MODE} port={PORT} allow={len(entries)} entries")
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, entries), "0.0.0.0", PORT
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
