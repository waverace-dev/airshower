import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { SafeError, strictJSON } from '../desktop/server.mjs';

let busy = false;
export async function ownerPrompt(prompt, signal) {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new SafeError('human_review_unavailable', 'This native owner prompt currently requires an Apple silicon Mac.');
  if (busy) throw new SafeError('review_busy', 'An Airshower owner prompt is already open. Finish it before opening another.');
  if (signal?.aborted) throw new SafeError('cancelled', 'Owner review was cancelled.');
  busy = true;
  try {
    return await new Promise((resolve, reject) => {
      const child = spawn(fileURLToPath(new URL('./native/Airshower Approval.app/Contents/MacOS/AirshowerApproval', import.meta.url)), [], { stdio: ['pipe', 'pipe', 'ignore'] });
      const chunks = []; let length = 0, stopped = false;
      const cancel = () => { stopped = true; child.kill(); };
      const timer = setTimeout(cancel, 5 * 60 * 1000);
      signal?.addEventListener('abort', cancel, { once: true });
      child.stdin.on('error', () => {});
      child.stdout.on('data', chunk => { length += chunk.length; if (length > 32768) cancel(); else chunks.push(chunk); });
      child.once('error', () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); reject(new SafeError('human_review_unavailable', 'The native owner prompt could not open. No permission was granted.')); });
      child.once('close', code => {
        clearTimeout(timer); signal?.removeEventListener('abort', cancel);
        if (stopped || signal?.aborted) return resolve({ action: 'cancel' });
        try {
          const value = strictJSON(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
          if (code !== 0 || !value || !['approve', 'deny', 'cancel', 'answer'].includes(value.action) || Object.keys(value).some(k => !['action', 'text', 'path'].includes(k)) || value.text !== undefined && typeof value.text !== 'string' || value.path !== undefined && typeof value.path !== 'string') throw new Error();
          resolve(value);
        } catch { reject(new SafeError('human_review_unavailable', 'The native owner prompt did not return a valid decision. No permission was granted.')); }
      });
      child.stdin.end(JSON.stringify(prompt));
    });
  } finally { busy = false; }
}
