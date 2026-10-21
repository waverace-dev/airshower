"""Build reproducible desktop packages from an explicit source allowlist.

Uses only the standard library. Never includes local profiles, credentials,
development caches, or a user's host configuration.
"""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def bundle(destination, entries):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for target, source in sorted(entries.items()):
            if source.is_symlink() or not source.is_file():
                raise ValueError("Package source must be a regular file: " + str(source))
            info = zipfile.ZipInfo(target, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Package integrity check failed")
    return {"file": destination.name, "bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def build(output):
    desktop = ROOT / "integrations" / "desktop"
    manifest = json.loads((desktop / "manifest.json").read_text())
    version = manifest["version"]
    shared = {"server/server.mjs": desktop / "server.mjs", "LICENSE": ROOT / "LICENSE"}
    results = [bundle(output / ("airshower-" + version + ".mcpb"), {
        **shared, "manifest.json": desktop / "manifest.json",
        "PRIVACY.md": ROOT / "docs" / "privacy.md",
    })]
    codex = ROOT / "integrations" / "codex"
    if (codex / "plugin.json").is_file():
        entries = dict(shared)
        for name in ("plugin.json", "mcp.json", "skills/airshower/SKILL.md"):
            entries[name] = codex / name
        results.append(bundle(output / ("airshower-codex-" + version + ".zip"), entries))
    (output / "SHA256SUMS").write_text("".join(item["sha256"] + "  " + item["file"] + "\n" for item in results))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    print(json.dumps(build(parser.parse_args().output), indent=2))
