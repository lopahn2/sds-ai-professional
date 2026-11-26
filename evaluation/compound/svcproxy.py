"""A Service's data path: each new TCP connection goes to a random current endpoint (kube-proxy iptables mode).

Endpoint changes affect only new connections; established ones keep their backend, as conntrack does.
Control: `GET /` lists endpoints, `PUT /` with a comma-separated body replaces them. Runs inside a container.
"""
import asyncio
import os
import random

LISTEN = int(os.environ.get("LISTEN", "5432"))
TARGET_PORT = int(os.environ.get("TARGET_PORT", "5432"))
endpoints = [e for e in os.environ.get("ENDPOINTS", "").split(",") if e]


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def serve(client_reader, client_writer):
    if not endpoints:
        client_writer.close()  # no ready endpoints: reject
        return
    try:
        reader, writer = await asyncio.open_connection(random.choice(endpoints), TARGET_PORT)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(pipe(client_reader, writer), pipe(reader, client_writer))


async def control(reader, writer):
    global endpoints
    head = await reader.readuntil(b"\r\n\r\n")
    method = head.split(b" ", 1)[0]
    length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length")), 0)
    body = (await reader.readexactly(length)).decode() if length else ""
    if method == b"PUT":
        endpoints = [e.strip() for e in body.split(",") if e.strip()]
    payload = ",".join(endpoints).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(payload), payload))
    await writer.drain()
    writer.close()


async def main():
    await asyncio.start_server(serve, "0.0.0.0", LISTEN)
    await asyncio.start_server(control, "0.0.0.0", 9000)
    await asyncio.Event().wait()


asyncio.run(main())
