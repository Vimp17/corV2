"""Build the Windows archive of the standalone build (no Docker on the server).

Runs on the build machine (Linux) inside a Python 3.12 container — see standalone/build/build.sh.
The result is one zip: unpack it on Windows Server and run mai.cmd. The database is Microsoft SQL
Server (app/db.py); the archive holds no database server. Inside:

  runtime/            official portable Python 3.12.10 for Windows (NuGet package "python",
                      signed by the Python Software Foundation) with every package already
                      installed: no internet, pip or administrator rights are needed on the server;
  app/ dashboard/ config/ n8n/ examples/documents/   — as in the repository;
  standalone/         the standalone layer (entry point, management commands, .env template);
  mai.cmd, README-DEMO.md, BUILD_INFO.json.

Steps: tools (uv, pefile) → Python package (SHA-512 checked against nuget.org) → Windows wheels
from standalone/requirements-windows.lock (requirements.txt resolved with the versions
tested in the Docker image) → bytecode →
check that every extension module finds its DLLs → project files → zip and its SHA-256.

Downloads go to standalone/dist/cache and are reused by the next build.
"""
from __future__ import annotations

import argparse
import base64
import compileall
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(os.environ.get("MAI_BUILD_SRC", Path(__file__).resolve().parents[2]))
DIST = Path(os.environ.get("MAI_BUILD_DIST", SRC / "standalone" / "dist"))
CACHE = DIST / "cache"
TOOLS = CACHE / "tools" / "uvenv"
PYTHON_VERSION = "3.12.10"
NUPKG_URL = f"https://www.nuget.org/api/v2/package/python/{PYTHON_VERSION}"
NUPKG_CATALOG = f"https://api.nuget.org/v3/registration5-semver1/python/{PYTHON_VERSION}.json"
PLATFORM = "x86_64-pc-windows-msvc"
LOCK = SRC / "standalone" / "requirements-windows.lock"
REQUIREMENTS = SRC / "requirements.txt"
APP_NAME = "mai-demo"
# Project files copied into the build (directories are copied without caches and tests).
# No scripts/ (sample data for tests and demos) and no README.md of the repository: README-DEMO.md
# describes the build.
PROJECT_ITEMS = ["app", "dashboard", "config", "n8n", "examples/documents"]
STANDALONE_ITEMS = ["__init__.py", "asgi.py", "envfile.py", "manage.py", "env.template",
                    "README.md"]
TEXT_CRLF = {".cmd", ".md", ".txt", ".template"}
# DLLs every supported Windows (Server 2016 and later) has: system libraries and the Universal CRT.
SYSTEM_DLLS = {
    "kernel32", "kernelbase", "user32", "gdi32", "gdiplus", "advapi32", "shell32", "ole32", "oleaut32", "ws2_32",
    "wsock32", "mswsock", "msvcrt", "ntdll", "rpcrt4", "bcrypt", "ncrypt", "crypt32", "secur32", "sspicli",
    "iphlpapi", "dnsapi", "winhttp", "wininet", "shlwapi", "comdlg32", "comctl32", "winmm", "version", "dbghelp",
    "psapi", "setupapi", "userenv", "normaliz", "wldap32", "imm32", "uxtheme", "dwmapi", "msimg32", "opengl32",
    "glu32", "d2d1", "d3d11", "dxgi", "dwrite", "windowscodecs", "mpr", "netapi32", "powrprof", "propsys",
    "cfgmgr32", "ucrtbase", "wintrust", "oleacc", "usp10", "winspool.drv", "shcore", "authz", "fwpuclnt",
    "msi", "cabinet", "dhcpcsvc", "credui", "wtsapi32", "pdh", "tdh", "virtdisk", "wevtapi", "bthprops.cpl",
    "bcryptprimitives", "synchronization", "odbc32",
}


def log(text: str) -> None:
    print(f"[build] {text}", flush=True)


def run(command: list[str], **kwargs) -> None:
    log(" ".join(str(part) for part in command))
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def download(url: str, target: Path) -> Path:
    if target.is_file() and target.stat().st_size:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    log(f"download {url}")
    with urllib.request.urlopen(url, timeout=900) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out, 1024 * 1024)
    partial.replace(target)
    return target


def tools() -> Path:
    uv = TOOLS / "bin" / "uv"
    if not uv.is_file():
        run([sys.executable, "-m", "venv", TOOLS])
        run([TOOLS / "bin" / "python", "-m", "pip", "install", "-q", "--retries", "10", "--timeout", "120", "uv", "pefile"])
    return uv


def python_runtime(target: Path) -> str:
    """Unpack the NuGet Python into target; returns the package SHA-256 (after the SHA-512 check)."""
    package = download(NUPKG_URL, CACHE / "downloads" / f"python.{PYTHON_VERSION}.nupkg")
    data = package.read_bytes()
    with urllib.request.urlopen(NUPKG_CATALOG, timeout=300) as response:
        leaf = json.load(response)
    entry = leaf.get("catalogEntry")
    if isinstance(entry, str):
        with urllib.request.urlopen(entry, timeout=300) as response:
            entry = json.load(response)
    expected = (entry or {}).get("packageHash")
    algorithm = str((entry or {}).get("packageHashAlgorithm") or "SHA512").lower()
    if not expected:
        raise SystemExit("nuget.org did not return the package hash: the Python package cannot be verified")
    actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if actual != expected:
        raise SystemExit(f"Python package hash mismatch: {actual} != {expected} (nuget.org)")
    log(f"python {PYTHON_VERSION}: {algorithm} matches nuget.org")
    with zipfile.ZipFile(package) as archive:
        for member in archive.infolist():
            if not member.filename.startswith("tools/") or member.is_dir():
                continue
            relative = member.filename[len("tools/"):]
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)
    if not (target / "python.exe").is_file():
        raise SystemExit("python.exe not found in the NuGet package")
    return hashlib.sha256(data).hexdigest()


def relock(uv: Path, constraints: Path | None) -> None:
    # uv writes through a temporary file beside the output: the source tree may be read-only, so
    # the lock is resolved into the cache and copied into place.
    resolved = CACHE / "requirements-windows.lock.new"
    command = [uv, "pip", "compile", REQUIREMENTS, "--python-platform", PLATFORM,
               "--python-version", "3.12", "--no-header", "--no-annotate", "-o", resolved]
    if constraints:
        command += ["--constraint", constraints]
    run(command, env={**os.environ, "UV_CACHE_DIR": str(CACHE / "uv")})
    header = ("# Windows (x86_64, Python 3.12) packages of the standalone build, resolved by\n"
              "# standalone/build/build.sh --relock from requirements.txt with the versions of\n"
              "# the tested Docker image. Edit by relocking, not by hand.\n")
    LOCK.write_text(header + resolved.read_text(encoding="utf-8"), encoding="utf-8")
    resolved.unlink()


def install_packages(uv: Path, site_packages: Path) -> None:
    run([uv, "pip", "install", "--target", site_packages, "--python-platform", PLATFORM, "--python-version", "3.12",
         "--only-binary", ":all:", "--no-deps", "-r", LOCK],
        env={**os.environ, "UV_CACHE_DIR": str(CACHE / "uv")})
    # Console scripts generated for the build machine (Linux) are of no use on Windows.
    shutil.rmtree(site_packages / "bin", ignore_errors=True)


def check_dlls(runtime: Path) -> list[str]:
    """Imported DLLs Windows would not find; returns readable problems.

    A DLL counts as found the way Python 3.8+ loads extension modules: in the module's own folder,
    beside python.exe (and runtime/DLLs), in the ".libs" folder its package registers with
    os.add_dll_directory (delvewheel), or among the libraries of Windows itself."""
    sys.path.insert(0, str(next((TOOLS / "lib").glob("python3*/site-packages"))))
    import pefile

    site_packages = runtime / "Lib" / "site-packages"
    names_in: dict[Path, set[str]] = {}
    files = [path for path in runtime.rglob("*") if path.suffix.lower() in {".dll", ".pyd"}]
    for path in files:
        names_in.setdefault(path.parent, set()).add(path.name.lower())
    problems: dict[str, set[str]] = {}
    for path in files:
        folders = {path.parent, runtime, runtime / "DLLs"}
        try:
            top = path.relative_to(site_packages).parts[0]
            folders.add(site_packages / f"{top.split('.')[0]}.libs")
            folders.add(site_packages / f"{top}.libs")
        except ValueError:
            pass
        reachable = set().union(*(names_in.get(folder, set()) for folder in folders))
        try:
            image = pefile.PE(str(path), fast_load=True)
            image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                                      pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        except pefile.PEFormatError:
            continue
        entries = list(getattr(image, "DIRECTORY_ENTRY_IMPORT", [])) + list(getattr(image, "DIRECTORY_ENTRY_DELAY_IMPORT", []))
        for entry in entries:
            name = entry.dll.decode("ascii", errors="replace").lower()
            base = name[:-4] if name.endswith(".dll") else name
            if (name in reachable or base in SYSTEM_DLLS or name in SYSTEM_DLLS
                    or name.startswith(("api-ms-win-", "ext-ms-"))):
                continue
            problems.setdefault(name, set()).add(str(path.relative_to(runtime.parent)))
        image.close()
    return [f"{dll}: {', '.join(sorted(users)[:4])}{' …' if len(users) > 4 else ''}"
            for dll, users in sorted(problems.items())]


def compile_bytecode(root: Path) -> None:
    """Bytecode for Python 3.12: libraries with unchecked hashes (valid whatever the file times in
    the zip), the application with checked hashes (an edited .py is recompiled)."""
    import py_compile

    unchecked = py_compile.PycInvalidationMode.UNCHECKED_HASH
    checked = py_compile.PycInvalidationMode.CHECKED_HASH
    for folder, mode in ((root / "runtime" / "Lib", unchecked), (root / "app", checked),
                         (root / "standalone", checked)):
        compileall.compile_dir(str(folder), quiet=1, invalidation_mode=mode, workers=0,
                               rx=re.compile(r"[\\/](test|tests|idle_test)[\\/]"))


def copy_project(root: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests")
    for item in PROJECT_ITEMS:
        source = SRC / item
        target = root / item
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (root / "standalone").mkdir(exist_ok=True)
    for item in STANDALONE_ITEMS:
        shutil.copy2(SRC / "standalone" / item, root / "standalone" / item)
    shutil.copy2(SRC / "standalone" / "mai.cmd", root / "mai.cmd")
    shutil.copy2(SRC / "standalone" / "README.md", root / "README-DEMO.md")
    for folder in ("data/documents", "inbox", "logs", "run", "pgdata"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for path in [root / "mai.cmd", root / "README-DEMO.md", root / "standalone" / "env.template"]:
        data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        path.write_bytes(data)


def build_info(root: Path, nupkg_sha256: str) -> dict:
    packages = [line.strip() for line in LOCK.read_text(encoding="utf-8").splitlines()
                if re.match(r"^[A-Za-z0-9_.\-]+==", line.strip())]
    commit = os.environ.get("MAI_BUILD_GIT_COMMIT", "unknown")
    info = {
        "version": datetime.now(timezone.utc).strftime("%Y.%m.%d") + "-" + commit[:7],
        "git_commit": commit, "git_branch": os.environ.get("MAI_BUILD_GIT_BRANCH", "unknown"),
        "git_dirty": os.environ.get("MAI_BUILD_GIT_DIRTY", "unknown"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": f"{PYTHON_VERSION} (NuGet python, win_amd64)", "python_package_sha256": nupkg_sha256,
        "database": "Microsoft SQL Server 2016+ (pyodbc with ODBC Driver 17/18, or pymssql)",
        "platform": "Windows x64 (Windows Server 2016 and later)", "packages": packages,
    }
    (root / "BUILD_INFO.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return info


def make_zip(root: Path, info: dict) -> Path:
    archive = DIST / f"{APP_NAME}-win64-{info['version']}.zip"
    partial = archive.with_suffix(".zip.part")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for path in sorted(root.rglob("*")):
            name = f"{APP_NAME}/{path.relative_to(root).as_posix()}"
            if path.is_dir():
                if not any(path.iterdir()):
                    out.writestr(name + "/", b"")
                continue
            out.write(path, name)
    partial.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--relock", action="store_true", help="resolve the Windows packages again")
    parser.add_argument("--constraints", type=Path, help="versions to keep when relocking (e.g. pip freeze "
                                                           "of the tested Docker image)")
    parser.add_argument("--no-zip", action="store_true", help="stop after the build folder")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Build with Python 3.12 (bytecode must match the Windows runtime)")
    uv = tools()
    if args.relock or not LOCK.is_file() or not LOCK.read_text(encoding="utf-8").strip():
        relock(uv, args.constraints)
    root = DIST / "build" / APP_NAME
    shutil.rmtree(root.parent, ignore_errors=True)
    root.mkdir(parents=True)
    nupkg_sha256 = python_runtime(root / "runtime")
    site_packages = root / "runtime" / "Lib" / "site-packages"
    install_packages(uv, site_packages)
    problems = check_dlls(root / "runtime")
    if problems:
        log("DLLs not shipped and not part of Windows (needs Microsoft Visual C++ Redistributable 2015-2022 x64):")
        for problem in problems:
            log("  " + problem)
    else:
        log("every extension module finds its DLLs (Python runtime, package folders or Windows)")
    copy_project(root)
    compile_bytecode(root)
    info = build_info(root, nupkg_sha256)
    info["dll_problems"] = problems
    (DIST / "last-build.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.no_zip:
        log(f"build folder: {root}")
        return 0
    archive = make_zip(root, info)
    log(f"archive: {archive} ({archive.stat().st_size / 1048576:.0f} MB)")
    log(f"sha256: {archive.with_suffix('.zip.sha256').read_text().split()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
