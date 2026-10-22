"""Build Apple silicon desktop previews from explicit, private-data-free inputs."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P2P = ROOT / 'integrations/p2p'

def bundle(destination, entries):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for target, source in sorted(entries.items()):
            if source.is_symlink() or not source.is_file():
                raise ValueError('Missing regular build input: ' + str(source))
            info = zipfile.ZipInfo(target, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = target.endswith(('/airshower-lock', '/AirshowerApproval', '/runtime/node'))
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            archive.writestr(info, source.read_bytes())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError('Archive integrity check failed')
    return {'file': destination.name, 'bytes': destination.stat().st_size, 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}

def build(output):
    entries = {'server/desktop/server.mjs': ROOT / 'integrations/desktop/server.mjs', 'LICENSE': ROOT / 'LICENSE',
               'PRIVACY.md': ROOT / 'docs/p2p-desktop.md', 'background-agent.md': ROOT / 'docs/background-agent.md',
               'THIRD_PARTY_NOTICES.md': P2P / 'THIRD_PARTY_NOTICES.md',
               'licenses/IROH-MIT.txt': P2P / 'licenses/IROH-MIT.txt',
               'dependencies.lock.json': P2P / 'dependencies.lock.json', 'node-runtime.lock.json': P2P / 'node-runtime.lock.json'}
    for name in ('mcp.mjs', 'engine.mjs', 'transport.mjs', 'storage.mjs', 'lease.mjs', 'owner-prompt.mjs', 'agent-runtime.mjs', 'background-agent.mjs', 'native/airshower-lock',
                 'native/Airshower Approval.app/Contents/Info.plist', 'native/Airshower Approval.app/Contents/MacOS/AirshowerApproval',
                 'native/Airshower Approval.app/Contents/_CodeSignature/CodeResources'):
        entries['server/p2p/' + name] = P2P / name
    for name in ('@number0/iroh/index.js', '@number0/iroh/package.json', '@number0/iroh-darwin-arm64/package.json', '@number0/iroh-darwin-arm64/iroh.darwin-arm64.node'):
        entries['server/p2p/node_modules/' + name] = P2P / 'node_modules' / name
    version = json.loads((P2P / 'manifest.json').read_text())['version']
    runtime = ROOT / 'build/p2p-node-darwin-arm64'
    checks = json.loads((runtime / 'verified.json').read_text())
    for name in ('bin/node', 'LICENSE'):
        if hashlib.sha256((runtime / name).read_bytes()).hexdigest() != checks[name]:
            raise ValueError('Runtime build input changed after verification')
    results = [bundle(output / ('airshower-p2p-' + version + '-macos-arm64.mcpb'), {**entries, 'manifest.json': P2P / 'manifest.json'})]
    codex = dict(entries)
    codex.update({'server/runtime/node': runtime / 'bin/node', 'licenses/NODE-LICENSE.txt': runtime / 'LICENSE'})
    for name in ('plugin.json', 'mcp.json', 'skills/airshower/SKILL.md'):
        codex[name] = P2P / 'codex' / name
    results.append(bundle(output / ('airshower-p2p-codex-' + version + '-macos-arm64.zip'), codex))
    (output / 'SHA256SUMS').write_text(''.join(item['sha256'] + '  ' + item['file'] + '\n' for item in results))
    return results

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/p2p')
    print(json.dumps(build(parser.parse_args().output), indent=2))
