import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON = ["--noconfirm", "--windowed", "--hidden-import", "pystray._win32", "--hidden-import", "win32timezone",
          "--collect-submodules", "dxcam", "--collect-submodules", "comtypes",
          "--exclude-module", "PIL._avif", "--exclude-module", "PIL.AvifImagePlugin",
          "--exclude-module", "comtypes.test", "--exclude-module", "tkinter.test", "--exclude-module", "pydoc_data"]


def _pyinstaller(out, *args, script="anykir.py", server=False):
    import logo
    os.makedirs(out, exist_ok=True)
    icon = os.path.join(out, "anykirserver.ico" if server else "anykir.ico")
    logo.save_ico(icon, server=server)
    marker = os.path.join(out, "anykir.bundle")
    with open(marker, "w", encoding="ascii") as f:
        f.write("AnyKir")
    subprocess.run([sys.executable, "-m", "PyInstaller", *COMMON, *args, "--icon", icon,
                    "--add-data", f"{marker}{os.pathsep}.",
                    "--distpath", os.path.join(out, "dist"), "--workpath", os.path.join(out, "work"),
                    "--specpath", out, os.path.join(HERE, script)], cwd=HERE, check=True)


def build(out):
    out = os.path.abspath(out)
    svc = os.path.join(out, "svc")
    _pyinstaller(svc, "--onedir", "--name", "AnyKirSvc")
    folder = os.path.join(svc, "dist", "AnyKirSvc")
    _check_same_libraries(os.path.join(folder, "_internal"))
    _pyinstaller(out, "--onefile", "--name", "AnyKir",
                 "--add-data", f"{os.path.join(folder, 'AnyKirSvc.exe')}{os.pathsep}svc")
    _check_service_files(os.path.join(out, "dist", "AnyKir.exe"), os.path.join(folder, "_internal"))
    return os.path.join(out, "dist", "AnyKir.exe")


def _check_same_libraries(internal):
    if not os.path.isfile(os.path.join(internal, "python312.dll")) and not any(
            n.startswith("python3") and n.endswith(".dll") for n in os.listdir(internal)):
        raise SystemExit(f"service build looks incomplete: {internal}")


def _check_service_files(exe, internal):
    from PyInstaller.archive.readers import CArchiveReader
    toc = {name.replace("\\", "/") for name in CArchiveReader(exe).toc}
    missing = []
    for root, _, files in os.walk(internal):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), internal).replace("\\", "/")
            if rel not in toc:
                missing.append(rel)
    if missing:
        raise SystemExit(f"AnyKir.exe lacks {len(missing)} files the service needs, e.g. {missing[:5]}")


def build_server(out):
    out = os.path.abspath(out)
    _pyinstaller(os.path.join(out, "server"), "--onefile", "--name", "AnyKirServer", script="server.py", server=True)
    return os.path.join(out, "server", "dist", "AnyKirServer.exe")


def main():
    exe = build(os.path.join(HERE, "build_all"))
    dist = os.path.join(HERE, "dist", "AnyKir.exe")
    os.makedirs(os.path.dirname(dist), exist_ok=True)
    try:
        shutil.copy2(exe, dist)
    except OSError:
        sys.exit(f"{dist} is in use (quit AnyKir first). The new build is at {exe}")
    print(f"\nDone: {dist}  ({os.path.getsize(dist) / 1e6:.0f} MB, includes the administrator service)")
    server = build_server(os.path.join(HERE, "build_all"))
    target = os.path.join(HERE, "dist", "AnyKirServer.exe")
    try:
        shutil.copy2(server, target)
        print(f"Done: {target}  ({os.path.getsize(target) / 1e6:.0f} MB)")
    except OSError:
        print(f"{target} is in use (quit AnyKir Server first). The new build is at {server}")


if __name__ == "__main__":
    main()
