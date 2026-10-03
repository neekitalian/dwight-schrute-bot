"""Build or verify a deterministic, committed-source Dwight toolkit ZIP.

No network, package installation, private data, model artifacts, or Git metadata
are included. Source selection uses committed Git blobs, never working files.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tomllib
from zipfile import ZipFile, ZipInfo, ZIP_STORED

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/neekitalian/dwight-schrute-bot"
MANIFEST = "release-manifest.json"
MAX_BYTES = 100_000_000
MAX_FILE_BYTES = 10_000_000
MAX_FILES = 5000
ROOT_FILES = {
    "LICENSE", "NOTICE", "README.md", "pyproject.toml", "Dockerfile",
    ".gitignore", ".dockerignore", ".env.example", "requirements.lock",
    "requirements-rl-env.lock",
}
REQUIRED = {"LICENSE", "NOTICE", "docs/VWAP-LICENSE", "pyproject.toml",
            "dwight/__init__.py", "dwight/__main__.py", "vwap_bot/engine.py"}
PRIVATE_PARTS = {".git", ".venv", "__pycache__", "private-data", "runs", "models",
                 "mlruns", "releases", "secrets", "raw", "node_modules",
                 "private-notes", "internal-business"}
# Publication boundaries use explicit paths, not keywords that could hide
# legitimate trading exits, market-data pricing or technical product docs.
PRIVATE_FILES = {"docs/product-strategy.md"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"hf_[A-Za-z0-9]{25,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)


def _git(root, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=root, stderr=subprocess.PIPE)


def allowed_path(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or name != str(path)
            or any(part in ("", ".", "..") or part.lower() in PRIVATE_PARTS for part in path.parts)):
        return False
    if name.lower() in PRIVATE_FILES:
        return False
    if name in ROOT_FILES or name == "docs/VWAP-LICENSE":
        return True
    if any(part.startswith(".") for part in path.parts):
        return False
    if name in {"examples/tradingview/qqq_observer.pine", "deploy/linux/tradingview-nginx.conf.example", "examples/github-actions-tests.yml"}:
        return True
    if name == "examples/manual-fills.csv":
        return True  # Contents are also required to explicitly declare synthetic.
    if len(path.parts) < 2:
        return False
    first = path.parts[0]
    if first in ("dwight", "vwap_bot", "tests"):
        return path.suffix == ".py"
    if first == "docs":
        return path.suffix == ".md"
    if first == "scripts":
        return path.suffix in (".py", ".sh")
    if first == "examples":
        return path.suffix == ".py" or name == "examples/config.json"
    if first == "configs":
        return path.suffix == ".json" and len(path.parts) == 2 and "model" not in path.stem.lower()
    if first == "deploy":
        return path.suffix in (".py", ".md", ".yaml", ".yml", ".service", ".timer", ".sh") or path.name == "requirements.txt"
    return False


def _check_content(name, content):
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"release file exceeds size limit: {name}")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise ValueError(f"credential-shaped content found; review committed file: {name}")
    if name == ".env.example":
        for line in content.decode("utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            key, separator, assigned = value.partition("=")
            if not separator:
                raise ValueError("invalid .env.example entry")
            if re.search(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", key, re.I) and assigned.strip():
                raise ValueError(".env.example contains a nonempty credential placeholder")
    if name == "examples/manual-fills.csv":
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        if not rows or any(row.get("data_kind") != "synthetic" for row in rows):
            raise ValueError("manual-fill example must contain synthetic rows only")


def _project(blobs):
    project = tomllib.loads(blobs["pyproject.toml"].decode("utf-8"))["project"]
    version = project.get("version", "")
    if project.get("name") != "dwight-schrute-bot" or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", version):
        raise ValueError("unexpected project name or unsafe static version")
    if not isinstance(project.get("requires-python"), str):
        raise ValueError("project must declare its Python requirement")
    return project


def _zip_entry(name, data, mode=0o644):
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = ZIP_STORED
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info, data


def build_release(output, root=ROOT, revision="HEAD"):
    """Return local ZIP/checksum paths after verifying the built source bundle."""
    root, output = Path(root).resolve(), Path(output).resolve()
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
    timestamp = int(_git(root, "show", "-s", "--format=%ct", commit).decode().strip())
    blobs, modes = {}, {}
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        header, encoded_name = record.split(b"\t", 1)
        name = encoded_name.decode("utf-8")
        if not allowed_path(name):
            continue
        mode, kind, oid = header.decode().split()
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError(f"release source must be a regular committed file: {name}")
        content = _git(root, "cat-file", "blob", oid)
        _check_content(name, content)
        blobs[name] = content
        modes[name] = 0o755 if mode == "100755" else 0o644
    if not REQUIRED <= blobs.keys():
        raise ValueError(f"required committed files missing: {sorted(REQUIRED-blobs.keys())}")
    if len(blobs) > MAX_FILES or sum(map(len, blobs.values())) > MAX_BYTES:
        raise ValueError("release source exceeds bundle limits")
    project = _project(blobs)
    name = f"dwight-toolkit-{project['version']}-{commit[:12]}"
    manifest = {
        "schema_version": 1, "artifact_type": "source_toolkit", "name": name,
        "project": project["name"], "version": project["version"],
        "requires_python": project["requires-python"], "source_commit": commit,
        "source_commit_timestamp": timestamp, "source_repository": REPOSITORY,
        "license": "Apache-2.0", "attribution_files": ["LICENSE", "NOTICE", "docs/VWAP-LICENSE"],
        "includes_private_data": False, "includes_model_artifacts": False,
        "broker_execution_enabled_by_install": False,
        "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                         "mode": oct(modes[path])} for path, data in sorted(blobs.items())},
    }
    contents = {**blobs, MANIFEST: (json.dumps(manifest, sort_keys=True, indent=2)+"\n").encode()}
    output.mkdir(parents=True, exist_ok=True)
    archive_path, checksum_path = output/f"{name}.zip", output/f"{name}.zip.sha256"
    if archive_path.exists() or checksum_path.exists():
        raise ValueError("release output already exists; choose a new output directory")
    created = False
    try:
        with ZipFile(archive_path, "x") as archive:
            created = True
            for path, data in sorted(contents.items()):
                info, data = _zip_entry(f"{name}/{path}", data, modes.get(path, 0o644))
                archive.writestr(info, data)
        verified = verify_release(archive_path)
        checksum_path.write_text(f"{verified['sha256']}  {archive_path.name}\n", encoding="ascii")
        return {**verified, "archive": str(archive_path), "checksum": str(checksum_path)}
    except Exception:
        if created:
            archive_path.unlink(missing_ok=True)
        raise


def verify_release(archive_path, expected_sha256=None):
    """Verify offline without extracting or executing any bundle code.

    A supplied digest must come from a separately trusted channel. An internal
    manifest/self-published checksum alone does not authenticate the publisher.
    """
    archive_path = Path(archive_path)
    if archive_path.stat().st_size > MAX_BYTES+5_000_000:
        raise ValueError("release archive exceeds size limit")
    raw = archive_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("release ZIP checksum mismatch")
    with ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > MAX_FILES+1 or len(names) != len(set(names)):
            raise ValueError("duplicate or excessive archive members")
        if sum(entry.file_size for entry in entries) > MAX_BYTES:
            raise ValueError("unpacked source exceeds size limit")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or "\\" in entry.filename or ".." in path.parts
                    or entry.filename != str(path) or len(path.parts) < 2
                    or entry.file_size > MAX_FILE_BYTES or entry.flag_bits & 1
                    or entry.compress_type != ZIP_STORED
                    or stat.S_IFMT(entry.external_attr >> 16) != stat.S_IFREG):
                raise ValueError("unsafe release member")
        manifests = [name for name in names if name.endswith("/"+MANIFEST)]
        if len(manifests) != 1:
            raise ValueError("release must contain one manifest")
        manifest = json.loads(archive.read(manifests[0]))
        if manifest.get("schema_version") != 1 or manifest.get("artifact_type") != "source_toolkit":
            raise ValueError("unsupported release manifest")
        prefix = manifest.get("name", "")
        commit = manifest.get("source_commit", "")
        version = manifest.get("version", "")
        if (not re.fullmatch(r"[a-f0-9]{40}", commit)
                or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", version)
                or prefix != f"dwight-toolkit-{version}-{commit[:12]}"):
            raise ValueError("invalid release identity")
        recorded = manifest.get("files")
        if not isinstance(recorded, dict) or not REQUIRED <= recorded.keys():
            raise ValueError("release lacks required source/license files")
        if set(names) != {f"{prefix}/{name}" for name in [*recorded, MANIFEST]}:
            raise ValueError("archive file roster differs from manifest")
        blobs = {}
        for name, record in recorded.items():
            if not allowed_path(name):
                raise ValueError(f"file outside release allowlist: {name}")
            content = archive.read(f"{prefix}/{name}")
            mode = stat.S_IMODE(archive.getinfo(f"{prefix}/{name}").external_attr >> 16)
            if (hashlib.sha256(content).hexdigest() != record.get("sha256")
                    or len(content) != record.get("bytes") or oct(mode) != record.get("mode")
                    or mode not in (0o644, 0o755)):
                raise ValueError(f"source file checksum/metadata mismatch: {name}")
            _check_content(name, content)
            blobs[name] = content
        project = _project(blobs)
        if (project["version"] != version or project["name"] != manifest.get("project")
                or project["requires-python"] != manifest.get("requires_python")
                or manifest.get("license") != "Apache-2.0"
                or manifest.get("includes_private_data") is not False
                or manifest.get("includes_model_artifacts") is not False
                or manifest.get("broker_execution_enabled_by_install") is not False):
            raise ValueError("release project/scope metadata mismatch")
    return {"verified": True, "name": prefix, "version": version, "source_commit": commit,
            "files": len(recorded), "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build from committed blobs; no network")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--revision", default="HEAD")
    verify = commands.add_parser("verify", help="Verify ZIP offline without extraction")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--expected-sha256")
    args = parser.parse_args()
    try:
        result = (build_release(args.output, revision=args.revision) if args.command == "build"
                  else verify_release(args.archive, args.expected_sha256))
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
