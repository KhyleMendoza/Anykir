import argparse
import base64
import json
import os
import glob
import re
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives import serialization

import build_exe
import updater

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(os.path.expanduser("~"), ".anykir-signing", "private.pem")
FEED_HOST = "192.168.210.112"
MOBILE = os.path.join(HERE, "mobile")
TOOLS = os.path.join(os.path.expanduser("~"), "dev")


def build_android(version):
    with open(os.path.join(MOBILE, "lib", "version.dart"), "w", encoding="utf-8") as f:
        f.write(f"// Set by publish.py; same numbering as the desktop AnyKir.\nconst appVersion = '{version}';\n")
    major, minor, patch = updater.parse_version(version)
    env = dict(os.environ)
    if not env.get("JAVA_HOME"):
        jdks = sorted(glob.glob(os.path.join(TOOLS, "jdk", "jdk-17*")))
        if jdks:
            env["JAVA_HOME"] = jdks[-1]
    env.setdefault("ANDROID_HOME", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"))
    flutter = shutil.which("flutter") or os.path.join(TOOLS, "flutter", "bin", "flutter.bat")
    command = [flutter, "build", "apk", "--release", "--build-name", version,
               "--build-number", str(major * 1_000_000 + minor * 1000 + patch)]
    subprocess.run(subprocess.list2cmdline(command), cwd=MOBILE, env=env, check=True, shell=True)
    return os.path.join(MOBILE, "build", "app", "outputs", "flutter-apk", "app-release.apk")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="new version, e.g. 1.2.0 (must be higher than the last release)")
    ap.add_argument("--no-dist", action="store_true", help="do not copy the exe to dist/AnyKir.exe")
    ap.add_argument("--feed", metavar="FOLDER", help="also write latest.json + the exe to this folder "
                    "(a web server's folder, a synced or shared folder) so PCs can update from there")
    ap.add_argument("--no-android", action="store_true", help="do not build and publish the phone app")
    ap.add_argument("--source", action="append", default=[], metavar="URL_OR_FOLDER",
                    help="extra update location to build into the exe (repeatable)")
    args = ap.parse_args()
    if updater.parse_version(args.version) is None:
        sys.exit("Version must look like 1.2.0")
    if not os.path.exists(KEY_PATH):
        sys.exit(f"Signing key not found: {KEY_PATH}")
    key = serialization.load_pem_private_key(open(KEY_PATH, "rb").read(), None)
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    if pub != updater.PUBLIC_KEY:
        sys.exit("The signing key does not match PUBLIC_KEY in updater.py; installed copies would reject the update.")

    try:
        from network_info import is_tailscale, local_ips
        tailscale = [f"http://{ip}:{updater.FEED_PORT}" for ip in local_ips() if is_tailscale(ip)][:1]
    except Exception:
        tailscale = []
    sources = list(dict.fromkeys([f"http://{FEED_HOST}:{updater.FEED_PORT}"] + tailscale + args.source))
    upd = os.path.join(HERE, "updater.py")
    text = open(upd, encoding="utf-8").read()
    text = re.sub(r"^DEFAULT_SOURCES = .*$", "DEFAULT_SOURCES = " + repr(sources), text, count=1, flags=re.M)
    open(upd, "w", encoding="utf-8").write(text)
    print("Update sources built into this release:", sources)

    common = os.path.join(HERE, "common.py")
    text = open(common, encoding="utf-8").read()
    text = re.sub(r'^VERSION = "[^"]*"', f'VERSION = "{args.version}"', text, count=1, flags=re.M)
    open(common, "w", encoding="utf-8").write(text)

    exe = build_exe.build(os.path.join(HERE, "build_release"))

    sig = base64.b64encode(key.sign(updater.signed_message(args.version, updater.file_sha256(exe)))).decode()
    updater.install_release(exe, args.version, sig)
    assert updater.verify(os.path.join(updater.release_dir(), updater.EXE_NAME), args.version, sig)
    print(f"\nRelease {args.version} built, signed and kept in {updater.release_dir()}")

    server_exe = None if args.no_dist else build_exe.build_server(os.path.join(HERE, "build_release"))

    apk = apk_sig = None
    if not args.no_android and os.path.isdir(MOBILE):
        apk = build_android(args.version)
        apk_sig = base64.b64encode(key.sign(updater.android_message(args.version, updater.file_sha256(apk)))).decode()
        updater.install_android(apk, args.version, apk_sig)
        print(f"Phone app {args.version} built and signed; phones update from the feed on port {updater.FEED_PORT}")

    if args.feed:
        os.makedirs(args.feed, exist_ok=True)
        name = f"AnyKir-{args.version}.exe"
        shutil.copy2(exe, os.path.join(args.feed, name))
        tmp = os.path.join(args.feed, "latest.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": args.version, "sig": sig, "file": name}, f)
        os.replace(tmp, os.path.join(args.feed, "latest.json"))
        if apk:
            apk_name = f"AnyKir-{args.version}.apk"
            shutil.copy2(apk, os.path.join(args.feed, apk_name))
            with open(os.path.join(args.feed, "android.json"), "w", encoding="utf-8") as f:
                json.dump({"version": args.version, "sig": apk_sig, "file": apk_name,
                           "size": os.path.getsize(apk)}, f)
        print(f"Feed written to {args.feed}")
    if args.no_dist:
        return
    if apk:
        shutil.copy2(apk, os.path.join(HERE, "dist", "AnyKir.apk"))
    dist = os.path.join(HERE, "dist", "AnyKir.exe")
    try:
        os.makedirs(os.path.dirname(dist), exist_ok=True)
        shutil.copy2(exe, dist)
        print(f"Also copied to {dist}")
    except OSError:
        print(f"{dist} is in use; AnyKir on this PC will update itself the next time it starts.")
    if server_exe:
        install_server(server_exe)
    print("Now connect to a host from this PC (or let one connect to you): AnyKir offers the update.")


def install_server(new_exe):
    import time
    import psutil
    dist = os.path.join(HERE, "dist", "AnyKirServer.exe")
    target = os.path.normcase(os.path.abspath(dist))
    running = []
    for p in psutil.process_iter(["exe"]):
        try:
            if p.info["exe"] and os.path.normcase(p.info["exe"]) == target:
                running.append(p)
        except (psutil.Error, OSError):
            pass
    for p in running:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(running, timeout=10)
    for _ in range(20):
        try:
            shutil.copy2(new_exe, dist)
            break
        except OSError:
            time.sleep(0.5)
    else:
        print(f"Could not replace {dist}; the new server is at {new_exe}")
        return
    print(f"AnyKir Server copied to {dist}")
    if running:
        subprocess.Popen([dist, "--minimized"], close_fds=True, creationflags=0x00000008 | 0x00000200)
        print("AnyKir Server restarted on the new version")


if __name__ == "__main__":
    main()
