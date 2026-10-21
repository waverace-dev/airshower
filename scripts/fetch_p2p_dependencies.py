"""Fetch pinned Iroh packages into the development tree; never run package scripts."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import platform
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 64 * 1024 * 1024


def fetch(target, output):
    lock = json.loads((ROOT / 'integrations/p2p/dependencies.lock.json').read_text())
    names = {'@number0/iroh', '@number0/iroh-' + target}
    selected = [entry for entry in lock['packages'] if entry['name'] in names]
    if len(selected) != 2:
        raise ValueError('This platform has no pinned package')
    for entry in selected:
        if not entry['url'].startswith('https://registry.npmjs.org/@number0/'):
            raise ValueError('Unexpected package registry')
        with urllib.request.urlopen(entry['url'], timeout=30) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT or 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode() != entry['integrity']:
            raise ValueError('Package integrity check failed')
        destination = output / entry['name']
        if destination.exists():
            raise ValueError('Destination already exists; preserve or explicitly choose another output directory: ' + str(destination))
        entries = []
        total = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0] != 'package':
                    raise ValueError('Unsafe package path')
                if member.isdir():
                    continue
                if not member.isfile() or member.size > LIMIT:
                    raise ValueError('Only bounded regular package files are accepted')
                total += member.size
                if total > LIMIT:
                    raise ValueError('Expanded package exceeds limit')
                entries.append((Path(*name.parts[1:]), archive.extractfile(member).read()))
        if len({str(path) for path, _ in entries}) != len(entries):
            raise ValueError('Duplicate package path')
        for relative, content in entries:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(content)
        print(entry['name'] + '@' + entry['version'] + ' integrity verified')


if __name__ == '__main__':
    default = 'darwin-arm64' if platform.system() == 'Darwin' and platform.machine() == 'arm64' else 'linux-x64-gnu'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('darwin-arm64', 'linux-x64-gnu'), default=default)
    parser.add_argument('--output', type=Path, default=ROOT / 'integrations/p2p/node_modules')
    args = parser.parse_args()
    fetch(args.target, args.output)
