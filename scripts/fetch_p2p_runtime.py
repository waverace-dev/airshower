"""Fetch the pinned Node runtime for the self-contained Codex macOS package.

Extracts only node and its full license notices, into build/. No npm, install
scripts, global PATH changes, app registration, or host configuration.
"""
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def fetch():
    lock = json.loads((ROOT / 'integrations/p2p/node-runtime.lock.json').read_text())
    destination = ROOT / 'build/p2p-node-darwin-arm64'
    if destination.exists():
        raise ValueError('Runtime destination exists; preserve it and use the existing verified build input.')
    if not lock['url'].startswith('https://nodejs.org/dist/'):
        raise ValueError('Unexpected runtime host')
    with urllib.request.urlopen(lock['url'], timeout=60) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != lock['sha256']:
        raise ValueError('Runtime archive integrity check failed')
    prefix = 'node-' + lock['version'] + '-darwin-arm64/'
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for name in lock['files']:
            member = archive.getmember(prefix + name)
            if not member.isfile() or member.size > 256 * 1024 * 1024:
                raise ValueError('Unexpected runtime entry')
            contents[name] = archive.extractfile(member).read()
    for name, raw in contents.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(raw)
        target.chmod(0o755 if name == 'bin/node' else 0o644)
    (destination / 'verified.json').write_text(json.dumps({name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}, indent=2) + '\n')
    print('Verified project-local Node ' + lock['version'] + '; no runtime installed globally.')

if __name__ == '__main__':
    fetch()
