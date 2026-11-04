import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives import serialization

import build_exe
import updater

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(os.path.expanduser("~"), ".anykir-signing", "private.pem")


def tailnet_ip():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for exe in (os.path.join(pf, "Tailscale", "tailscale.exe"), "tailscale"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.SubprocessError):
            continue
        for x in out:
            if x.startswith("100."):
                return x
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="new version, e.g. 1.2.0 (must be higher than the last release)")
    ap.add_argument("--no-dist", action="store_true", help="do not copy the exe to dist/AnyKir.exe")
    ap.add_argument("--feed", metavar="FOLDER", help="also write latest.json + the exe to this folder "
                    "(a web server's folder, a synced or shared folder) so PCs can update from there")
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

    sources = list(args.source)
    ip = tailnet_ip()
    if ip:
        sources.insert(0, f"http://{ip}:{updater.FEED_PORT}")
    if sources:
        upd = os.path.join(HERE, "updater.py")
        text = open(upd, encoding="utf-8").read()
        text = re.sub(r"^DEFAULT_SOURCES = .*$", "DEFAULT_SOURCES = " + repr(sources), text, count=1, flags=re.M)
        open(upd, "w", encoding="utf-8").write(text)
        print("Update sources built into this release:", sources)
    else:
        print("No Tailscale address found and no --source given: PCs will only update when they connect.")

    common = os.path.join(HERE, "common.py")
    text = open(common, encoding="utf-8").read()
    text = re.sub(r'^VERSION = "[^"]*"', f'VERSION = "{args.version}"', text, count=1, flags=re.M)
    open(common, "w", encoding="utf-8").write(text)

    exe = build_exe.build(os.path.join(HERE, "build_release"))

    sig = base64.b64encode(key.sign(updater.signed_message(args.version, updater.file_sha256(exe)))).decode()
    updater.install_release(exe, args.version, sig)
    assert updater.verify(os.path.join(updater.release_dir(), updater.EXE_NAME), args.version, sig)
    print(f"\nRelease {args.version} built, signed and kept in {updater.release_dir()}")

    if args.feed:
        os.makedirs(args.feed, exist_ok=True)
        name = f"AnyKir-{args.version}.exe"
        shutil.copy2(exe, os.path.join(args.feed, name))
        tmp = os.path.join(args.feed, "latest.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": args.version, "sig": sig, "file": name}, f)
        os.replace(tmp, os.path.join(args.feed, "latest.json"))
        print(f"Feed written to {args.feed}")
    if args.no_dist:
        return
    dist = os.path.join(HERE, "dist", "AnyKir.exe")
    try:
        os.makedirs(os.path.dirname(dist), exist_ok=True)
        shutil.copy2(exe, dist)
        print(f"Also copied to {dist}")
    except OSError:
        print(f"{dist} is in use; AnyKir on this PC will update itself the next time it starts.")
    print("Now connect to a host from this PC (or let one connect to you): AnyKir offers the update.")


if __name__ == "__main__":
    main()
