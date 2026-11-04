import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = ["--noconfirm", "--windowed", "--hidden-import", "pystray._win32", "--hidden-import", "win32timezone",
          "--collect-submodules", "dxcam", "--collect-submodules", "comtypes"]


def _pyinstaller(out, *args):
    subprocess.run([sys.executable, "-m", "PyInstaller", *COMMON, *args,
                    "--distpath", os.path.join(out, "dist"), "--workpath", os.path.join(out, "work"),
                    "--specpath", out, os.path.join(HERE, "anykir.py")], cwd=HERE, check=True)


def build(out):
    out = os.path.abspath(out)
    svc = os.path.join(out, "svc")
    _pyinstaller(svc, "--onedir", "--name", "AnyKirSvc")
    folder = os.path.join(svc, "dist", "AnyKirSvc")
    payload = os.path.join(svc, "AnyKirSvc.zip")
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, svc + os.sep + "dist"))
    script = os.path.join(HERE, "install-service.ps1")
    _pyinstaller(out, "--onefile", "--name", "AnyKir",
                 "--add-data", f"{payload}{os.pathsep}svc", "--add-data", f"{script}{os.pathsep}svc")
    return os.path.join(out, "dist", "AnyKir.exe")


def main():
    exe = build(os.path.join(HERE, "build_all"))
    dist = os.path.join(HERE, "dist", "AnyKir.exe")
    os.makedirs(os.path.dirname(dist), exist_ok=True)
    try:
        shutil.copy2(exe, dist)
    except OSError:
        sys.exit(f"{dist} is in use (quit AnyKir first). The new build is at {exe}")
    print(f"\nDone: {dist}  ({os.path.getsize(dist) / 1e6:.0f} MB, includes the administrator service)")


if __name__ == "__main__":
    main()
