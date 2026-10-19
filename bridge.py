#!/usr/bin/env python3
"""
ocbox service bridge: forwards a FIXED list of TCP ports to the SAME ports on the host.

Why: your dev services (Postgres, RabbitMQ, Garage, ...) run elsewhere (e.g. Docker /
Portainer) and publish ports on the host. The agent has no route to the host, so this
tiny container sits on both networks and relays only the ports you listed in
[services.ports] of ocbox.toml, to the host address in [services].host.

It cannot be used to reach any other host or port: the target host is fixed and the
port must be on the list. Stdlib only.

Env:  FORWARD_PORTS=5432,5672   TARGET_HOST=host.containers.internal
"""
import asyncio
import os
import sys

TARGET = os.environ.get("TARGET_HOST", "host.containers.internal")
PORTS = [int(p) for p in os.environ.get("FORWARD_PORTS", "").split(",") if p.strip()]


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


def make_handler(port):
    async def handle(reader, writer):
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(TARGET, port), 5)
        except Exception as exc:
            log(f"FAIL port={port} target={TARGET}: {type(exc).__name__}: {exc}")
            writer.close()
            return
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
    return handle


async def main():
    if not PORTS:
        sys.exit("no FORWARD_PORTS given")
    servers = [await asyncio.start_server(make_handler(p), "0.0.0.0", p) for p in PORTS]
    log(f"bridge up: ports={PORTS} target={TARGET}")
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
