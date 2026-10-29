"""Stage the CI-built Vite console and bind every nested asset to a manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


def stage(source: Path, destination: Path) -> dict:
    expected = (Path.cwd() / "src/medplain/static/console").resolve()
    if destination.resolve() != expected:
        raise ValueError("console destination must be this checkout's src/medplain/static/console")
    if not (source / "index.html").is_file():
        raise ValueError("Vite did not produce index.html")
    if "/console/" not in (source / "index.html").read_text(encoding="utf-8"):
        raise ValueError("Vite must use base /console/")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("console build may not contain symlinks")
    if not any(path.suffix == ".js" for path in files):
        raise ValueError("console build has no executable JavaScript")
    manifest = {
        "base": "/console/",
        "files": {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
    }
    # The resolved target is checked above before deleting generated assets.
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    (destination / "_console_manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {"asset_count": len(files), "destination": str(destination)}


def verify_archive(archive_path: Path, console: Path) -> dict:
    manifest = json.loads((console / "_console_manifest.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(archive_path) as archive:
        for name, digest in manifest["files"].items():
            packaged = archive.read("medplain/static/console/" + name)
            if hashlib.sha256(packaged).hexdigest() != digest:
                raise ValueError(f"packaged console asset differs from CI build: {name}")
    return {"archive": archive_path.name, "verified_console_assets": len(manifest["files"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("web/dist"))
    parser.add_argument("--destination", type=Path, default=Path("src/medplain/static/console"))
    parser.add_argument("--verify-archive", type=Path)
    args = parser.parse_args()
    result = verify_archive(args.verify_archive, args.destination) if args.verify_archive else stage(args.source, args.destination)
    print(json.dumps(result))
