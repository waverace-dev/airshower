// Opt-in: contacts public n0 discovery/relays with temporary fictional peers.
// Never runs as part of the offline test glob. No keys or cards are logged.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { PeerEngine } from '../engine.mjs';
import { bounded } from '../transport.mjs';

const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'airshower-internet-fixture-')));
const engines = [];
const make = async label => {
  const engine = await new PeerEngine({ directory: path.join(root, label), name: 'Fictional internet test ' + label,
    timeout: 20000, retryMs: 60000, approve: async () => ({ action: 'approve' }) }).start();
  engines.push(engine);
  console.log(JSON.stringify({ stage: 'waiting_for_public_relay', fixture: label }));
  try { await bounded(engine.transport.endpoint.online(), 90000); }
  catch (error) { console.log(JSON.stringify({ stage: 'public_relay_unavailable', fixture: label, configured_relay: engine.transport.endpoint.addr().relayUrl() })); throw error; }
  console.log(JSON.stringify({ stage: 'public_relay_connected', fixture: label }));
  return engine;
};
try {
  const a = await make('A'); let b = await make('B');
  await a.connect(b.transport.card(b.data.name)); await b.connect(a.transport.card(a.data.name));
  const send = key => a.send({ recipient_agent_id: b.id, kind: 'task', message: 'Harmless automated Airshower connectivity fixture; no real task.', idempotency_key: key });
  const r = await send('first'); assert.equal(r.status, 'pending_approval');
  await b.review(r.id); b.claim(r.id); await b.complete(r.id, 'Fixture acknowledged.'); await a.sync();
  assert.equal(a.requestRow(r.id).result.text, 'Fixture acknowledged.');
  const initialPaths = [...a.transport.paths.values()].flat();
  if (process.argv.includes('--require-relay')) assert.ok(initialPaths.some(p => p.selected && p.type === 'relay'), 'Expected the application result to use a selected relay path');
  const oldId = b.id; await b.close();
  const queued = await send('restart'); assert.equal(queued.delivered, false);
  b = await make('B'); assert.equal(b.id, oldId);
  // Do not refresh either stored card. Prove restart recovery with discovery
  // and the original pinned identity/routes, rather than a new pairing.
  for (let i = 0; i < 4 && !a.requestRow(queued.id).delivered; i++) await a.sync();
  assert.equal(b.requestRow(queued.id).status, 'pending_approval');
  assert.equal(Object.keys(b.data.requests).length, 2);
  console.log(JSON.stringify({ test: 'public-infrastructure-fixtures', request_reply: 'passed', restart_with_original_cards: 'passed', initial_paths: initialPaths,
    two_physical_macs: false, desktop_host_installation: false, require_relay: process.argv.includes('--require-relay') }));
} finally {
  await Promise.all(engines.map(e => e.close())); fs.rmSync(root, { recursive: true, force: true });
}
