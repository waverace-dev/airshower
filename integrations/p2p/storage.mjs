import fs from 'node:fs';
import path from 'node:path';
import { randomUUID, randomBytes } from 'node:crypto';
import { SafeError, strictJSON } from '../desktop/server.mjs';
import { acquireLease } from './lease.mjs';

const fail = (message) => { throw new SafeError('private_storage_error', message); };
export class PrivateStore {
  static async open(directory) {
    directory = path.resolve(directory);
    let current = directory; const parents = [];
    while (true) { parents.push(current); const next = path.dirname(current); if (next === current) break; current = next; }
    for (const item of parents.reverse()) {
      if (!fs.existsSync(item)) fs.mkdirSync(item, { mode: 0o700 });
      if (fs.lstatSync(item).isSymbolicLink()) fail('Profile paths must not contain symbolic links.');
    }
    PrivateStore.prototype.check(fs.lstatSync(directory), true);
    const lease = await acquireLease(path.join(directory, 'profile.lock'));
    try { return new PrivateStore(directory, lease); }
    catch (error) { await lease.close(); throw error; }
  }
  constructor(directory, lease) {
    this.directory = directory; this.lease = lease;
    if (!lease?.alive()) fail('An active exclusive profile lease is required.');
    this.filename = path.join(this.directory, 'p2p.json');
      this.data = fs.existsSync(this.filename) ? this.read(this.filename) : {
        version: 1, secret: randomBytes(32).toString('hex'), name: '', contacts: {}, requests: {}, sources: {},
      };
      if (this.data.version !== 1 || !/^[a-f0-9]{64}$/.test(this.data.secret) || !['contacts', 'requests', 'sources'].every(k => this.data[k] && typeof this.data[k] === 'object' && !Array.isArray(this.data[k]))) fail('Invalid P2P profile. Existing data was preserved.');
      this.save();
  }
  check(info, directory = false) {
    if (!(directory ? info.isDirectory() : info.isFile()) || info.mode & 0o077 || info.uid !== process.getuid() || (!directory && info.nlink !== 1)) fail('Use an owned private directory and private regular files. Existing permissions were preserved.');
  }
  read(filename) {
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    try {
      const info = fs.fstatSync(fd); this.check(info);
      if (info.size > 32 * 1024 * 1024) fail('The private profile exceeds its size limit.');
      const buffer = Buffer.alloc(32 * 1024 * 1024 + 1); let count = 0;
      while (count < buffer.length) { const n = fs.readSync(fd, buffer, count, buffer.length - count, null); if (!n) break; count += n; }
      if (count === buffer.length) fail('The private profile exceeds its size limit.');
      return strictJSON(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, count)));
    } finally { fs.closeSync(fd); }
  }
  save() {
    this.assertHealthy();
    try { this.write(); }
    catch (error) { this.failed = true; throw error; }
  }
  assertHealthy() { if (this.failed || !this.lease?.alive()) fail('Private storage is unavailable. Close and reopen the extension; the last action was not confirmed.'); }
  write() {
    const raw = JSON.stringify(this.data);
    if (Buffer.byteLength(raw) > 32 * 1024 * 1024) fail('The private profile has reached its capacity. Existing data was preserved.');
    if (fs.existsSync(this.filename)) this.check(fs.lstatSync(this.filename));
    const temporary = path.join(this.directory, '.save-' + randomUUID());
    const fd = fs.openSync(temporary, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY | fs.constants.O_NOFOLLOW, 0o600);
    try { fs.writeFileSync(fd, raw); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(temporary, this.filename);
    const dir = fs.openSync(this.directory, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
    try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
  }
  async close() { if (this.lease) { const lease = this.lease; this.lease = null; await lease.close(); } }
}
