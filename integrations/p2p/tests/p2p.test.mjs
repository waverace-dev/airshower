import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID, randomBytes, createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { PeerEngine } from '../engine.mjs';
import { PeerTransport } from '../transport.mjs';
import { PrivateStore } from '../storage.mjs';
import { P2PServer, TOOLS } from '../mcp.mjs';
import { valid } from '../../desktop/server.mjs';

const delay = ms => new Promise(r => setTimeout(r, ms));
async function fixture(t, count = 2) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'airshower-p2p-test-')));
  const engines = [], decisions = [], prompts = [];
  t.after(async () => { await Promise.all(engines.map(e => e.close())); fs.rmSync(root, { recursive: true, force: true }); });
  for (let i = 0; i < count; i++) {
    decisions[i] = { action: 'approve' }; prompts[i] = [];
    engines.push(await new PeerEngine({ directory: path.join(root, String(i)), name: 'Fixture ' + i, mode: 'local', timeout: 500, retryMs: 60000,
      approve: async p => { prompts[i].push(p); return typeof decisions[i] === 'function' ? decisions[i](p) : decisions[i]; },
    }).start());
  }
  const [a, b] = engines;
  const connect = async (x = a, y = b) => { await x.connect(y.transport.card(y.data.name)); await y.connect(x.transport.card(x.data.name)); };
  const send = (kind = 'task', extra = {}) => a.send({ recipient_agent_id: b.id, kind, message: 'A harmless fixture request', idempotency_key: randomUUID(), ...extra });
  return { root, engines, a, b, decisions, prompts, connect, send };
}

test('real encrypted direct peers require both pinned identities, preserve the exact owner decision, and push results', async t => {
  const { a, b, decisions, connect, send, prompts } = await fixture(t);
  await a.connect(b.transport.card(b.data.name));
  const queued = await send(); assert.equal(queued.delivered, false);
  assert.equal(Object.keys(b.data.requests).length, 0);
  await b.connect(a.transport.card(a.data.name)); await a.sync();
  assert.equal(b.requestRow(queued.id).status, 'pending_approval');
  assert.throws(() => b.claim(queued.id), { code: 'approval_required' });
  assert.equal((await b.review(queued.id)).status, 'ready');
  assert.match(prompts[1].at(-1).detail, /A harmless fixture request/);
  b.claim(queued.id); await b.complete(queued.id, 'Completed fixture');
  assert.equal(a.requestRow(queued.id).result.text, 'Completed fixture');
  assert.deepEqual(a.requestRow(queued.id).attribution, { kind: 'agent', endpoint_id: b.id });
  assert.ok([...a.transport.paths.values()].flat().some(p => p.type === 'direct' && p.selected));
  assert.ok(!JSON.stringify(a.status()).includes(a.data.secret));
  decisions[1] = { action: 'deny' };
  const denied = await send(); await b.review(denied.id); await a.sync();
  assert.equal(a.requestRow(denied.id).status, 'denied');
  assert.throws(() => b.claim(denied.id), { code: 'approval_required' });
});

test('local observers see persisted incoming work and permission changes without granting access', async t => {
  const { a, b, connect, send } = await fixture(t); await connect();
  const observed = [];
  b.subscribe(() => { throw new Error('A broken observer must not break delivery'); });
  const unsubscribe = b.subscribe(() => observed.push(JSON.parse(fs.readFileSync(b.store.filename, 'utf8'))));
  const request = await send();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(observed.some(data => data.requests[request.id]?.status === 'pending_approval'));
  assert.throws(() => b.claim(request.id), { code: 'approval_required' });
  await b.policy(a.id, ['task'], []);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(observed.at(-1).contacts[a.id].policy.kinds, ['task']);
  // Changing a standing policy does not retroactively approve an older request.
  assert.throws(() => b.claim(request.id), { code: 'approval_required' });
  await b.disconnect(a.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(observed.at(-1).contacts[a.id].active, false);
  unsubscribe(); const length = observed.length;
  b.notify(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(observed.length, length);
});

test('dismissal never approves; only exact human text can answer ask_human', async t => {
  const { a, b, decisions, connect, send } = await fixture(t); await connect();
  const r = await send('ask_human');
  assert.equal(r.status, 'waiting_human');
  await assert.rejects(b.complete(r.id, 'Pretend human answer'));
  decisions[1] = { action: 'cancel' }; await b.review(r.id); assert.equal(b.requestRow(r.id).status, 'waiting_human');
  decisions[1] = { action: 'approve' }; await assert.rejects(b.review(r.id), { code: 'invalid_human_response' });
  decisions[1] = { action: 'answer', text: 'My exact human answer.' }; await b.review(r.id);
  assert.equal(a.requestRow(r.id).result.text, 'My exact human answer.'); assert.equal(a.requestRow(r.id).attribution.kind, 'human');
});

test('cancellation during a visible owner review invalidates the decision', async t => {
  const { a, b, decisions, connect, send } = await fixture(t); await connect();
  const r = await send(); let release, entered;
  const opened = new Promise(resolve => { entered = resolve; });
  decisions[1] = () => new Promise(resolve => { release = resolve; entered(); });
  const reviewing = b.review(r.id); await opened; await a.cancel(r.id);
  release({ action: 'approve' }); await assert.rejects(reviewing, { code: 'review_changed' });
  assert.equal(b.requestRow(r.id).status, 'cancelled');
});

test('standing permissions require the owner and are rechecked after claim', async t => {
  const { a, b, decisions, connect, send } = await fixture(t); await connect();
  decisions[1] = { action: 'cancel' }; await b.policy(a.id, ['task'], []);
  assert.deepEqual(b.contact(a.id).policy.kinds, []);
  decisions[1] = { action: 'approve' }; await b.policy(a.id, ['task'], []);
  const r = await send(); assert.equal(r.status, 'ready'); b.claim(r.id);
  await b.policy(a.id, [], []); await assert.rejects(b.complete(r.id, 'Not allowed'), { code: 'approval_required' });
  await assert.rejects(b.policy(a.id, ['ask_human'], []), { code: 'invalid_arguments' });
});

test('published context is scoped to the exact contact and source; revocation stops reads', async t => {
  const { a, b, engines, connect, send } = await fixture(t, 3); const c = engines[2]; await connect(); await connect(c, b);
  const published = await b.publish(a.id, 'Fixture context', 'Only this deliberately shared text.');
  assert.deepEqual(b.receive(c.id, { version: 1, type: 'catalog' }), { sources: [] });
  const r = await send('context', { source_ids: [published.source.id] });
  assert.throws(() => b.readSources(r.id), { code: 'approval_required' });
  await b.review(r.id); assert.equal(b.readSources(r.id).sources[0].content, 'Only this deliberately shared text.');
  await b.revokeSource(published.source.id); assert.throws(() => b.readSources(r.id), { code: 'source_denied' });
  await assert.rejects(b.complete(r.id, 'Cannot use revoked context'), { code: 'source_denied' });
});

test('real encrypted file handoff verifies bytes, never overwrites the original and respects subsequent revocation', async t => {
  const { root, a, b, decisions, connect, send } = await fixture(t); await connect();
  const original = path.join(root, 'fixture.txt'), bytes = Buffer.from('Airshower file fixture\n'); fs.writeFileSync(original, bytes);
  decisions[1] = { action: 'approve', path: original };
  const { source } = await b.publish(a.id, 'Fixture file');
  const r = await send('file', { source_ids: [source.id] }); await b.review(r.id); b.claim(r.id); await b.complete(r.id, 'The approved file', [source.id]);
  const received = await a.download(r.id, source.id);
  assert.deepEqual(fs.readFileSync(received.path), bytes); assert.deepEqual(fs.readFileSync(original), bytes);
  assert.equal(fs.statSync(received.path).mode & 0o777, 0o600);
  assert.equal(received.sha256, createHash('sha256').update(bytes).digest('hex'));
  assert.equal((await a.download(r.id, source.id)).path, received.path);
  await b.revokeSource(source.id); await assert.rejects(a.download(r.id, source.id), { code: 'peer_rejected' });
});

test('standing file permission revocation blocks downloads even after a completed result', async t => {
  const { root, a, b, decisions, connect, send } = await fixture(t); await connect();
  const file = path.join(root, 'fixture.txt'); fs.writeFileSync(file, 'fixture'); decisions[1] = { action: 'approve', path: file };
  const { source } = await b.publish(a.id, 'Fixture file'); await b.policy(a.id, ['file'], [source.id]);
  const r = await send('file', { source_ids: [source.id] }); await b.complete(r.id, 'Fixture', [source.id]);
  await b.policy(a.id, [], []); await assert.rejects(a.download(r.id, source.id));
});

test('model paths, symlinks, oversized files and unintended source scopes fail closed', async t => {
  const { root, a, b, decisions, connect, send } = await fixture(t); await connect();
  const original = path.join(root, 'fixture'); fs.writeFileSync(original, 'x'); const link = path.join(root, 'link'); fs.symlinkSync(original, link);
  decisions[1] = { action: 'approve', path: link }; await assert.rejects(b.publish(a.id, 'Link'));
  const huge = path.join(root, 'huge'); const fd = fs.openSync(huge, 'w'); fs.ftruncateSync(fd, 5 * 1024 * 1024 + 1); fs.closeSync(fd);
  decisions[1] = { action: 'approve', path: huge }; await assert.rejects(b.publish(a.id, 'Too large'), { code: 'file_limit' });
  await assert.rejects(send('file'), { code: 'invalid_arguments' });
  await assert.rejects(send('task', { source_ids: [randomUUID()] }), { code: 'invalid_arguments' });
  assert.equal(Object.keys(b.data.sources).length, 0);
});

test('request identity, replay, terminal results, attribution and result file scopes are validated', async t => {
  const { a, b, connect, send } = await fixture(t); await connect();
  const r = await send(); const request = Object.fromEntries(['id', 'sender_agent_id', 'recipient_agent_id', 'kind', 'message', 'source_ids', 'idempotency_key', 'created_at', 'expires_at'].map(k => [k, r[k]]));
  assert.equal(b.receive(a.id, { version: 1, type: 'submit', request }).id, r.id);
  assert.throws(() => b.receive(a.id, { version: 1, type: 'submit', request: { ...request, sender_agent_id: b.id } }), { code: 'invalid_request' });
  assert.throws(() => b.receive(a.id, { version: 1, type: 'submit', request: { ...request, message: 'Changed replay' } }), { code: 'idempotency_conflict' });
  await b.review(r.id); await b.complete(r.id, 'Actual result');
  const snapshot = b.snapshot(b.requestRow(r.id));
  assert.throws(() => a.applySnapshot(b.id, { ...snapshot, revision: snapshot.revision + 1, result: { ...snapshot.result, text: 'Changed result' } }), { code: 'invalid_peer_response' });
  assert.throws(() => a.applySnapshot(b.id, { ...snapshot, attribution: { kind: 'human', endpoint_id: b.id } }), { code: 'invalid_peer_response' });
  assert.throws(() => a.applySnapshot(b.id, { ...snapshot, status: 'ready', result: undefined }), { code: 'invalid_peer_response' });
  assert.equal(Object.keys(b.data.requests).length, 1);
  assert.equal((await a.send({ recipient_agent_id: b.id, kind: r.kind, message: r.message, idempotency_key: r.idempotency_key })).id, r.id);
});

test('disconnect blocks future peer operations and in-progress claims', async t => {
  const { a, b, connect, send } = await fixture(t); await connect();
  const r = await send(); await b.review(r.id); b.claim(r.id); await b.disconnect(a.id);
  assert.throws(() => b.receive(a.id, { version: 1, type: 'get', request_id: r.id }), { code: 'not_connected' });
  await assert.rejects(b.complete(r.id, 'No longer allowed'), { code: 'not_connected' });
});

test('reconnecting a revoked contact does not resurrect old approvals or published files', async t => {
  const { root, a, b, decisions, connect, send } = await fixture(t); await connect();
  const file = path.join(root, 'fixture'); fs.writeFileSync(file, 'fixture'); decisions[1] = { action: 'approve', path: file };
  const { source } = await b.publish(a.id, 'Fixture');
  const task = await send(); await b.review(task.id);
  const handoff = await send('file', { source_ids: [source.id] }); await b.review(handoff.id); await b.complete(handoff.id, 'Fixture', [source.id]);
  await b.disconnect(a.id); await b.connect(a.transport.card(a.data.name));
  assert.equal(b.requestRow(task.id).status, 'denied'); assert.throws(() => b.claim(task.id));
  assert.deepEqual(b.receive(a.id, { version: 1, type: 'catalog' }), { sources: [] });
  await assert.rejects(a.download(handoff.id, source.id));
});

test('offline outbox and identity survive a restart, retries do not duplicate delivery', async t => {
  const { a, b, engines, connect, send } = await fixture(t); await connect();
  const identity = a.id, bIdentity = b.id; await b.close();
  const r = await send(); assert.equal(r.status, 'queued'); await a.close();
  const a2 = await new PeerEngine({ ...a.options, directory: a.directory, retryMs: 60000, approve: a.approve }).start(); engines.push(a2);
  const b2 = await new PeerEngine({ ...b.options, directory: b.directory, retryMs: 60000, approve: b.approve }).start(); engines.push(b2);
  assert.equal(a2.id, identity); assert.equal(b2.id, bIdentity);
  // Minimal local mode deliberately has no public discovery. Refresh routes for
  // the pinned keys; internet-mode discovery/reconnect has a separate smoke test.
  a2.data.contacts[b2.id].card = b2.transport.card(b2.data.name); b2.data.contacts[a2.id].card = a2.transport.card(a2.data.name);
  await a2.sync(); await a2.sync(); assert.equal(Object.keys(b2.data.requests).length, 1);
  assert.equal(b2.requestRow(r.id).status, 'pending_approval');
});

test('private profiles reject symlinks and shared modes; OS lease prevents simultaneous opens and releases on process death', async t => {
  const { root } = await fixture(t, 0); const directory = path.join(root, 'lease');
  const first = await PrivateStore.open(directory);
  await assert.rejects(PrivateStore.open(directory), { code: 'private_storage_error' });
  const secret = first.data.secret; await first.close();
  const second = await PrivateStore.open(directory); assert.equal(second.data.secret, secret); await second.close();
  fs.chmodSync(path.join(directory, 'p2p.json'), 0o644); await assert.rejects(PrivateStore.open(directory), { code: 'private_storage_error' }); fs.chmodSync(path.join(directory, 'p2p.json'), 0o600);
  const alias = path.join(root, 'alias'); fs.symlinkSync(directory, alias); await assert.rejects(PrivateStore.open(alias), { code: 'private_storage_error' });
  const child = spawn(process.execPath, ['--input-type=module', '-e', `import {PrivateStore} from ${JSON.stringify(new URL('../storage.mjs', import.meta.url).href)}; await PrivateStore.open(${JSON.stringify(directory)}); console.log('ready'); setInterval(()=>{},1000);`], { stdio: ['ignore', 'pipe', 'pipe'] });
  await new Promise((resolve, reject) => { child.stdout.once('data', resolve); child.once('error', reject); child.once('exit', code => reject(new Error('Lease fixture exited: ' + code))); });
  child.kill('SIGKILL'); await new Promise(resolve => child.once('exit', resolve));
  let recovered;
  for (let i = 0; i < 30; i++) { try { recovered = await PrivateStore.open(directory); break; } catch { await delay(50); } }
  assert.ok(recovered); assert.equal(recovered.data.secret, secret); await recovered.close();
});

test('cards reject mismatched keys, extra fields, self contacts and arbitrary relay URLs', async t => {
  const { a, b } = await fixture(t); const card = b.transport.card(b.data.name);
  assert.throws(() => a.transport.parseCard({ ...card, endpoint_id: a.id }));
  assert.throws(() => a.transport.parseCard({ ...card, secret: 'not allowed' }));
  assert.throws(() => a.transport.parseCard(a.transport.card(a.data.name)));
  const iroh = a.transport.iroh;
  const hostile = new iroh.EndpointAddr(iroh.EndpointId.fromString(b.id), 'https://127.0.0.1/', []);
  assert.throws(() => a.transport.parseCard({ ...card, ticket: iroh.EndpointTicket.fromAddr(hostile).toString() }));
});

test('late transport completion cannot issue a request after the deadline', async () => {
  let writes = 0, closed = false;
  const transport = new PeerTransport({ secret: randomBytes(32).toString('hex'), authorize: () => true, receive: () => {}, timeout: 10 });
  transport.iroh = { EndpointTicket: { fromString: () => ({ endpointAddr: () => ({}) }) } };
  transport.endpoint = { connect: async () => { await delay(50); return { close: () => { closed = true; }, remoteId: () => ({ toString: () => 'peer' }), openBi: () => { writes++; } }; } };
  await assert.rejects(transport.rpc({ endpoint_id: 'peer', ticket: 'fixture' }, {}), { code: 'peer_unavailable' });
  await delay(70); assert.equal(writes, 0); assert.equal(closed, true);
});

test('ordinary URL-only desktop MCP host exposes native review, rejects model approval/path/secrets and reports wakeup limits', async t => {
  const { root } = await fixture(t, 0); const directory = path.join(root, 'mcp');
  const server = new P2PServer({ env: {}, engineOptions: { directory, name: 'Fixture owner', mode: 'local', approve: async () => ({ action: 'cancel' }) } });
  t.after(() => server.close());
  const init = await server.handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: { elicitation: { url: {} } }, clientInfo: { name: 'Fixture Desktop', version: '1' } } });
  assert.equal(init.result.serverInfo.name, 'airshower-p2p');
  await server.handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  const tools = (await server.handle({ jsonrpc: '2.0', id: 2, method: 'tools/list' })).result.tools;
  assert.ok(tools.some(t => t.name === 'review_request')); assert.equal(fs.existsSync(directory), false);
  for (const [name, args] of [['review_request', { request_id: randomUUID(), approved: true }], ['publish_file', { peer_id: 'a'.repeat(64), label: 'x', path: '/etc/passwd' }], ['ready', { name: 'Fixture', secret: 'x' }]]) assert.equal((await server.callTool(name, args)).isError, true);
  assert.equal(fs.existsSync(directory), false);
  const ready = JSON.parse((await server.callTool('ready', {})).content[0].text); assert.equal(ready.autonomous_chat_wakeup, false); assert.equal(ready.account_server_required, false);
  assert.equal((await server.callTool('wait_for_request', { timeout_seconds: 0 })).isError, false);
});

test('MCP waiting is cancellable and never requires an idle-chat capability', async t => {
  const { root } = await fixture(t, 0); const server = new P2PServer({ env: {}, engineOptions: { directory: path.join(root, 'mcp'), name: 'Fixture', mode: 'local' } }); t.after(() => server.close());
  const controller = new AbortController(); const waiting = server.callTool('wait_for_request', { timeout_seconds: 45 }, controller.signal); setTimeout(() => controller.abort(), 50);
  const output = await waiting; assert.equal(output.isError, true); assert.equal(JSON.parse(output.content[0].text).error.code, 'cancelled');
  assert.ok(TOOLS.every(t => t.inputSchema.additionalProperties === false));
  assert.equal(valid({ name: 'x', profile: '/tmp/anything' }, TOOLS[0].inputSchema), false);
});
