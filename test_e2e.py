import os
import subprocess
import sys
import tempfile
import time

from common import AuthError, ConnectError
from viewer import ViewerSession

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
env = dict(os.environ, APPDATA=tempfile.mkdtemp())


def wait(cond, timeout=10, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.05)
    raise SystemExit(f"TIMEOUT waiting for {what}")


relay = subprocess.Popen([PY, "relay.py", "--port", "7100", "--key", "k"], cwd=HERE, env=env)
host = subprocess.Popen([PY, "host.py", "--nogui", "--relay", "127.0.0.1:7100", "--relay-key", "k",
                         "--listen", "7101", "--set-password", "correct-horse"], cwd=HERE, env=env,
                        stdout=subprocess.PIPE, text=True)
try:
    line = host.stdout.readline()
    hid = line.split("ID:")[1].split()[0]
    print("host id", hid)
    time.sleep(1.5)

    try:
        ViewerSession().connect(hid, "nope", relay="127.0.0.1:7100", relay_key="k")
        raise SystemExit("FAIL: wrong password accepted")
    except (ConnectError, AuthError) as e:
        print("wrong password rejected:", e)
    try:
        ViewerSession().connect(hid, "correct-horse", relay="127.0.0.1:7100", relay_key="bad")
        raise SystemExit("FAIL: bad relay key accepted")
    except ConnectError as e:
        print("bad relay key rejected:", e)

    for label, kwargs in (("relay", dict(relay="127.0.0.1:7100", relay_key="k")),
                          ("direct", dict(direct="127.0.0.1:7101"))):
        time.sleep(1.5)
        s = ViewerSession()
        s.connect("" if "direct" in kwargs else hid, "correct-horse", **kwargs)
        wait(lambda: s.fb is not None and s.frames > 0, 10, f"first frame ({label})")
        print(f"[{label}] screen {s.fb.size}, frames so far {s.frames}")

        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "hello.bin")
        data = os.urandom(1_500_000)
        open(src, "wb").write(data)
        s.upload(src, tmp + os.sep + "up")
        got = {}

        def drain():
            while not s.events.empty():
                ev = s.events.get()
                if ev[0] == "msg":
                    got[ev[1]["t"]] = ev[1]
            return got
        wait(lambda: "put_done" in drain(), 10, "put_done")
        assert got["put_done"]["ok"], got["put_done"]
        remote = os.path.join(tmp, "up", "hello.bin")
        assert open(remote, "rb").read() == data
        dest = os.path.join(tmp, "back.bin")
        s.download_file(remote, dest)
        wait(lambda: "get_done" in drain(), 10, "get_done")
        assert got["get_done"]["ok"] and open(dest, "rb").read() == data
        s.send({"t": "ls", "path": tmp})
        wait(lambda: "ls_result" in drain(), 5, "ls_result")
        names = [e[0] for e in got["ls_result"]["entries"]]
        assert "up" in names and "hello.bin" in names, names
        s.send({"t": "cfg", "scale": 0.5, "quality": 30})
        n = s.frames
        time.sleep(1.5)
        print(f"[{label}] upload/download/ls OK, size after scale {s.fb.size}")
        s.close()
    print("ALL OK")
finally:
    host.kill()
    relay.kill()
