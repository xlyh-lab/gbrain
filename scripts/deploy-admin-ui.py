#!/usr/bin/env python3
"""Deploy the pinned GBrain Observatory commit on its macOS host."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit


PIN = Path(__file__).with_name("admin-ui-revision")
JOBS = ("com.gbrain.admin-ui-reader", "com.gbrain.admin-ui")
HOST_FILES = ("oauth.json", "reader", "launchd.log", "launchd.err", "reader.log", "reader.err")
ASSETS = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
COMMIT = re.compile(r"[0-9a-f]{40}")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def stage(repo, revision, release, host):
    archive = release.parent / "dashboard.tar"
    archive.write_bytes(git(repo, "archive", "--format=tar", f"{revision}:macos"))
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            parts = Path(member.name).parts
            if (not parts or parts[0] in HOST_FILES or parts[0].startswith(".")
                    or ".." in parts or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsupported deployment archive entry: {member.name}")
        source.extractall(release, filter="data")

    metadata = json.loads((release / "runtime/compatibility.json").read_text())
    version, base, patch = (metadata[key] for key in ("version", "base", "patch"))
    if (not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version) or not COMMIT.fullmatch(base)
            or patch != Path(patch).name or not patch.endswith(".patch")):
        raise ValueError("Invalid dashboard runtime compatibility metadata")
    runtime = host / "runtime" / version
    if git(runtime / "source", "rev-parse", "HEAD").decode().strip() != base:
        raise ValueError(f"Expected GBrain runtime {version} at {base}")
    git(runtime / "source", "apply", "--reverse", "--check", str(release / "runtime" / patch))
    for binary in ("gbrain", "bun"):
        if not os.access(runtime / "bin" / binary, os.X_OK):
            raise ValueError(f"Missing runtime executable: {binary}")

    subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
                   cwd=release, check=True)
    subprocess.run(["node", "--check", "dist/app.js"], cwd=release, check=True)
    for file in ("server.py", "start-read-backend.py", "model_providers.py"):
        compile((release / file).read_bytes(), file, "exec")
    for file in ASSETS.values():
        if not (release / "dist" / file).stat().st_size:
            raise ValueError(f"Empty dashboard asset: {file}")


def restart():
    for job in JOBS:
        subprocess.run(["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{job}"], check=True)


def get(origin, path, body=None):
    command = ["/usr/bin/curl", "--fail", "--silent", "--max-time", "4"]
    if body is not None:
        command += ["--header", "Content-Type: application/json", "--data", json.dumps(body)]
    return subprocess.check_output([*command, origin + path])


def verify(origin, release):
    for attempt in range(8):
        try:
            for path, file in ASSETS.items():
                if get(origin, path) != (release / "dist" / file).read_bytes():
                    raise ValueError(f"Unexpected bytes for {path}")
            for path, body in (("/api/status", None),
                               ("/api/tool", {"name": "sources_list", "arguments": {}}),
                               ("/admin/api/model-routes", None)):
                response = json.loads(get(origin, path, body))
                if not isinstance(response, dict) or response.get("ok") is not True:
                    raise ValueError(f"Unhealthy endpoint: {path}")
            return
        except (subprocess.CalledProcessError, ValueError) as error:
            if attempt == 7:
                raise ValueError(f"Dashboard verification failed: {error}") from error
            time.sleep(1)


def activate(host, staged, revision, origin):
    active = host / "admin-ui"
    state = host / ".admin-ui-host"
    first_release = not active.is_symlink()
    if first_release and state.exists():
        raise ValueError(f"Resolve the interrupted deployment at {state} before retrying")
    previous = None if first_release else active.readlink()
    if not active.is_dir():
        raise ValueError(f"Missing installed dashboard: {active}")
    current_state = active if first_release else state
    credential = current_state / "oauth.json"
    if not credential.is_file() or stat.S_IMODE(credential.stat().st_mode) & 0o077:
        raise ValueError("The installed dashboard requires a private OAuth credential file")
    if not (current_state / "reader").is_dir():
        raise ValueError("The installed dashboard is missing its reader state directory")

    release = Path(tempfile.mkdtemp(prefix=f".admin-ui-{revision[:12]}-", dir=host))
    replacement = release.with_name(release.name + ".next")
    try:
        shutil.copytree(staged, release, dirs_exist_ok=True)
        (release / ".deployed-revision").write_text(revision + "\n")
        for name in HOST_FILES:
            (release / name).symlink_to(Path("..") / state.name / name)
        replacement.symlink_to(release.name)
        try:
            if first_release:
                active.rename(state)
            replacement.replace(active)
            restart()
            verify(origin, release)
        except (Exception, KeyboardInterrupt):
            if first_release and state.is_dir():
                if active.is_symlink():
                    active.unlink()
                state.rename(active)
                restart()
            elif not first_release and active.readlink() != previous:
                replacement.unlink(missing_ok=True)
                replacement.symlink_to(previous)
                replacement.replace(active)
                restart()
            raise
    finally:
        replacement.unlink(missing_ok=True)
        if active.resolve() != release.resolve():
            shutil.rmtree(release)
    print(f"Deployed dashboard {revision} at {origin}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-repo", type=Path, required=True)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Switch the installed dashboard after validation")
    parser.add_argument("--origin", help="Existing Tailscale HTTPS origin, required with --apply")
    options = parser.parse_args()
    if options.apply:
        origin = urlsplit(options.origin or "")
        if (origin.scheme != "https" or not origin.netloc or origin.username is not None
                or origin.path or origin.query or origin.fragment):
            parser.error("--apply requires --origin https://hostname:8443 without a trailing slash")
    revision = PIN.read_text().strip()
    if not COMMIT.fullmatch(revision):
        raise ValueError("The dashboard pin must be a full Git commit SHA")
    if options.apply:
        git(options.ui_repo, "fetch", "origin", "main:refs/remotes/origin/main")
        git(options.ui_repo, "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main")

    host = options.host_root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="gbrain-observatory-") as directory:
        staged = Path(directory) / "dashboard"
        stage(options.ui_repo, revision, staged, host)
        print(f"Validated dashboard {revision}", flush=True)
        if options.apply:
            with (host / ".admin-ui-deploy.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                activate(host, staged, revision, options.origin)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        sys.exit(str(error))
