import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { SafeError } from '../desktop/server.mjs';

export function acquireLease(filename) {
  return new Promise((resolve, reject) => {
    const child = spawn(fileURLToPath(new URL('./native/airshower-lock', import.meta.url)), [filename], { stdio: ['pipe', 'pipe', 'ignore'] });
    let ready = false, output = '';
    const exited = new Promise(done => child.once('close', done));
    const timer = setTimeout(() => { child.kill(); reject(new SafeError('private_storage_error', 'The profile lock helper did not start.')); }, 5000);
    child.stdin.on('error', () => {});
    child.once('error', () => { clearTimeout(timer); reject(new SafeError('private_storage_error', 'The bundled profile lock helper is unavailable.')); });
    child.stdout.on('data', data => {
      output += data.toString();
      if (!ready && output === 'ready\n') {
        ready = true; clearTimeout(timer);
        resolve({ child, close: async () => { child.stdin.end(); await exited; }, alive: () => child.exitCode === null && !child.killed });
      } else { child.kill(); clearTimeout(timer); reject(new SafeError('private_storage_error', 'Invalid profile lock response.')); }
    });
    child.once('exit', code => { clearTimeout(timer); if (!ready) reject(new SafeError('private_storage_error', code === 2 ? 'This P2P profile is already open in another host process. Close that connection before retrying.' : 'The private profile could not be locked.')); });
  });
}
