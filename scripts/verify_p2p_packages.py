"""Verify extracted release archives with a real two-process MCP roundtrip.

The harness injects fictional test decisions through constructor arguments.
There is no shipped test mode or environment-based approval bypass.
"""
import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent

class Host:
    def __init__(self, node, package, profile):
        self.sequence = 0
        harness = package / 'fixture-harness.mjs'
        harness.write_text('''import {P2PServer} from './server/p2p/mcp.mjs';
import {runStdio} from './server/desktop/server.mjs';
class Fixture extends P2PServer {
  constructor({send}) { super({send,env:{},engineOptions:{directory:process.argv[2],name:'Fictional package fixture',mode:'local',timeout:2000,retryMs:60000,approve:async()=>({action:'approve'})}}); }
}
runStdio(process.stdin,process.stdout,undefined,Fixture);
''')
        self.process = subprocess.Popen([str(node), str(harness), str(profile)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.call('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {'elicitation': {'url': {}}},
                                'clientInfo': {'name': 'Fictional package test', 'version': '1'}})
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.sequence, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        if not select.select([self.process.stdout], [], [], 15)[0]:
            raise AssertionError('Packaged MCP response timed out')
        response = json.loads(self.process.stdout.readline())
        assert response.get('id') == self.sequence and 'error' not in response, 'Invalid MCP response'
        return response['result']

    def tool(self, name, **arguments):
        result = self.call('tools/call', {'name': name, 'arguments': arguments})
        assert not result.get('isError'), 'Packaged tool failed: ' + name
        return json.loads(result['content'][0]['text'])

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise AssertionError('Packaged MCP process did not stop')
        assert self.process.returncode == 0, 'Packaged MCP exited unsuccessfully'
        assert not self.process.stderr.read(), 'Packaged MCP emitted unexpected diagnostics'

def verify(directory):
    with tempfile.TemporaryDirectory(prefix='airshower-package-test-') as temporary:
        root = Path(temporary).resolve()
        packages = []
        for index, suffix in enumerate(('.mcpb', '.zip')):
            archives = list(directory.glob('*' + suffix))
            assert len(archives) == 1, 'Expected exactly one package of each type'
            destination = root / str(index)
            with zipfile.ZipFile(archives[0]) as archive:
                assert archive.testzip() is None
                for entry in archive.infolist():
                    target = destination / entry.filename
                    assert not Path(entry.filename).is_absolute() and '..' not in Path(entry.filename).parts
                    assert target.name not in ('p2p.json', 'profile.lock', 'session.lock', '.env')
                    assert not entry.is_dir()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(entry))
                    target.chmod((entry.external_attr >> 16) & 0o777)
            app = destination / 'server/p2p/native/Airshower Approval.app'
            subprocess.run(['codesign', '--verify', '--strict', str(app)], check=True)
            subprocess.run([str(app / 'Contents/MacOS/AirshowerApproval'), '--version'], check=True, capture_output=True)
            packages.append(destination)
        node = packages[1] / 'server/runtime/node'
        version = subprocess.check_output([str(node), '--version'], text=True).strip()
        assert version == json.loads((ROOT / 'integrations/p2p/node-runtime.lock.json').read_text())['version']
        hosts = []
        try:
            a = Host(node, packages[0], root / 'profile-a'); hosts.append(a)
            b = Host(node, packages[1], root / 'profile-b'); hosts.append(b)
            aa, bb = a.tool('ready'), b.tool('ready')
            assert aa['account_server_required'] is False and bb['autonomous_chat_wakeup'] is False
            a.tool('connect_contact', card=b.tool('connection_card')['card'])
            b.tool('connect_contact', card=a.tool('connection_card')['card'])
            request = a.tool('request', recipient_agent_id=bb['endpoint_id'], kind='task', message='Harmless packaged fixture', idempotency_key='package-test')
            assert request['status'] == 'pending_approval'
            b.tool('review_request', request_id=request['id'])
            b.tool('claim_request', request_id=request['id'])
            b.tool('complete_request', request_id=request['id'], text='Package roundtrip verified')
            received = a.tool('get_request', request_id=request['id'])
            assert received['status'] == 'completed' and received['result']['text'] == 'Package roundtrip verified'
            assert received['attribution'] == {'kind': 'agent', 'endpoint_id': bb['endpoint_id']}
            print(json.dumps({'archive_integrity': 'passed', 'native_signature_integrity': 'passed',
                              'bundled_node': version, 'two_process_packaged_mcp_roundtrip': 'passed',
                              'real_desktop_installation': False, 'two_physical_macs': False}))
        finally:
            for host in hosts:
                host.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'dist/p2p')
    verify(parser.parse_args().directory)
