import argparse
import asyncio
import hmac
import json
import secrets
import time

hosts = {}
waiting = {}


async def read_json(reader):
    line = await asyncio.wait_for(reader.readline(), 10)
    if not line or len(line) > 2048:
        raise ValueError("bad request")
    return json.loads(line)


def send(writer, obj):
    writer.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle_host(reader, writer, msg, log):
    hid, sec = str(msg.get("id", "")), str(msg.get("sec", ""))
    if not hid.isdigit() or not (6 <= len(hid) <= 12) or len(sec) < 16:
        send(writer, {"ok": False, "err": "bad id"})
        return
    cur = hosts.get(hid)
    if cur and not hmac.compare_digest(cur[0], sec) and not cur[1].is_closing():
        send(writer, {"ok": False, "err": "id in use"})
        return
    if cur and cur[1] is not writer:
        cur[1].close()
    hosts[hid] = (sec, writer)
    send(writer, {"ok": True})
    await writer.drain()
    log(f"host {hid} online")
    try:
        while not writer.is_closing():
            await asyncio.sleep(25)
            send(writer, {"op": "ping"})
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        if hosts.get(hid, (None, None))[1] is writer:
            del hosts[hid]
        writer.close()
        log(f"host {hid} offline")


async def handle_connect(reader, writer, msg, log):
    hid = str(msg.get("id", ""))
    entry = hosts.get(hid)
    if not entry or entry[1].is_closing():
        send(writer, {"ok": False, "err": "host offline"})
        return
    token = secrets.token_hex(16)
    fut = asyncio.get_running_loop().create_future()
    waiting[token] = fut
    try:
        send(entry[1], {"op": "incoming", "token": token})
        await entry[1].drain()
        hr, hw = await asyncio.wait_for(fut, 12)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        send(writer, {"ok": False, "err": "host did not answer"})
        return
    finally:
        waiting.pop(token, None)
    send(writer, {"ok": True})
    send(hw, {"ok": True})
    await asyncio.gather(hw.drain(), writer.drain())
    log(f"session to {hid} started")
    t0 = time.time()
    await asyncio.gather(pipe(reader, hw), pipe(hr, writer))
    log(f"session to {hid} ended after {int(time.time() - t0)}s")


async def handle_accept(reader, writer, msg):
    fut = waiting.get(str(msg.get("token", "")))
    if fut and not fut.done():
        fut.set_result((reader, writer))
        return True
    send(writer, {"ok": False, "err": "unknown token"})
    return False


def make_handler(shared_key, log):
    async def handler(reader, writer):
        keep_open = False
        try:
            msg = await read_json(reader)
            if shared_key and not hmac.compare_digest(str(msg.get("key", "")), shared_key):
                send(writer, {"ok": False, "err": "bad relay key"})
                return
            op = msg.get("op")
            if op == "host":
                keep_open = True
                await handle_host(reader, writer, msg, log)
            elif op == "connect":
                keep_open = True
                await handle_connect(reader, writer, msg, log)
            elif op == "accept":
                keep_open = await handle_accept(reader, writer, msg)
        except (asyncio.TimeoutError, ValueError, ConnectionError, OSError):
            pass
        finally:
            if not keep_open:
                try:
                    await writer.drain()
                except Exception:
                    pass
                writer.close()
    return handler


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=7000)
    ap.add_argument("--key", default="", help="shared secret clients must present (recommended)")
    args = ap.parse_args()

    def log(text):
        print(time.strftime("%H:%M:%S"), text, flush=True)

    server = await asyncio.start_server(make_handler(args.key, log), args.host, args.port)
    log(f"relay listening on {args.host}:{args.port}" + ("" if args.key else "  (WARNING: no --key set)"))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
