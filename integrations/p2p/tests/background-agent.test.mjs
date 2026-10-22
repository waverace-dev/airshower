import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { PeerEngine } from '../engine.mjs';
import { BackgroundAgent } from '../background-agent.mjs';
import { runProcess, parseRuntimeResponse } from '../agent-runtime.mjs';

const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function until(predicate) {
  const end = Date.now() + 5000;
  while (!predicate()) { if (Date.now() > end) throw new Error('Background condition timed out'); await tick(); }
}
async function fixture(t, runtimeOptions = {}) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'airshower-background-test-')));
  const prompts = [], runs = []; let decision = { action: 'approve' };
  const a = await new PeerEngine({ directory: path.join(root, 'a'), name: 'Fixture A', mode: 'local', timeout: 1000, retryMs: 60000, approve: async () => ({ action: 'approve' }) }).start();
  const b = await new PeerEngine({ directory: path.join(root, 'b'), name: 'Fixture B', mode: 'local', timeout: 1000, retryMs: 60000,
    approve: async prompt => { prompts.push(prompt); return typeof decision === 'function' ? decision(prompt) : decision; },
  }).start();
  const runtime = { inspect: async provider => ({ provider, version: 'fictional fixture', binary: '/fictional/runtime' }),
    run: async (config, envelope) => { runs.push(envelope); return { outcome: 'reply', text: 'A scoped fixture reply' }; }, ...runtimeOptions };
  const worker = new BackgroundAgent(b, { runtime });
  t.after(async () => { await worker.close(); await Promise.all([a.close(), b.close()]); fs.rmSync(root, { recursive: true, force: true }); });
  await a.connect(b.transport.card(b.data.name)); await b.connect(a.transport.card(a.data.name));
  const send = (kind = 'message', extra = {}) => a.send({ recipient_agent_id: b.id, kind, message: 'A fictional incoming request', idempotency_key: randomUUID(), ...extra });
  return { root, a, b, worker, runtime, prompts, runs, send, decide: value => { decision = value; } };
}

test('idle receiver starts a helper from real P2P arrival and pushes a scoped answer without any inbox tool call', async t => {
  const { a, b, worker, runs, send } = await fixture(t);
  await b.policy(a.id, ['message'], []);
  await worker.enable('claude');
  const request = await send();
  await until(() => a.requestRow(request.id).status === 'completed');
  assert.equal(runs.length, 1);
  assert.deepEqual(Object.keys(runs[0]).sort(), ['kind', 'message', 'sources']);
  assert.equal(a.requestRow(request.id).result.text, 'A scoped fixture reply');
  assert.deepEqual(a.requestRow(request.id).attribution, { kind: 'agent', endpoint_id: b.id });
  await a.sync(); await b.sync(); await tick(); assert.equal(runs.length, 1);
  assert.ok(!JSON.stringify(a.public(a.requestRow(request.id))).includes('background'));
});

test('helper is off by default and declining enable or request approval never starts a model', async t => {
  const { worker, b, runs, send, decide } = await fixture(t);
  const request = await send(); await tick(); assert.equal(runs.length, 0);
  decide({ action: 'cancel' }); await worker.enable('codex');
  assert.equal(worker.status().enabled, false); assert.equal(runs.length, 0);
  decide(prompt => ({ action: prompt.title.includes('background helper') ? 'approve' : 'deny' }));
  await worker.enable('codex');
  await until(() => b.requestRow(request.id).status === 'denied');
  assert.equal(runs.length, 0);
});

test('a request arriving after the final queue scan is not stranded while drain settles', async t => {
  const { a, b, worker, send, runs } = await fixture(t);
  await b.policy(a.id, ['message'], []);
  const drain = worker.drain.bind(worker);
  let injected = false, request;
  worker.drain = async () => {
    await drain();
    if (!injected) {
      injected = true;
      // Deliver while the old drain promise still owns the running flag,
      // after it has already decided there is nothing else to process.
      request = await send();
    }
  };
  await worker.enable('codex');
  await until(() => request && a.requestRow(request.id).status === 'completed');
  assert.equal(runs.length, 1);
});

test('human questions wake native owner review and return the actual human text without a model', async t => {
  const { a, worker, runs, send, decide } = await fixture(t);
  await worker.enable('claude');
  decide({ action: 'answer', text: 'The exact fictional human answer.' });
  const request = await send('ask_human');
  await until(() => a.requestRow(request.id).status === 'completed');
  assert.equal(a.requestRow(request.id).result.text, 'The exact fictional human answer.');
  assert.equal(a.requestRow(request.id).attribution.kind, 'human'); assert.equal(runs.length, 0);
});

test('approved file requests finish automatically without model access to file bytes', async t => {
  const { root, a, b, worker, runs, send, decide } = await fixture(t);
  const source = path.join(root, 'fixture.txt'); fs.writeFileSync(source, 'Exact fixture bytes');
  decide({ action: 'approve', path: source }); const published = await b.publish(a.id, 'Fixture file');
  decide({ action: 'approve' });
  await b.policy(a.id, ['file'], [published.source.id]); await worker.enable('codex');
  const request = await send('file', { source_ids: [published.source.id] });
  await until(() => a.requestRow(request.id).status === 'completed');
  assert.equal(runs.length, 0);
  const received = await a.download(request.id, published.source.id);
  assert.equal(fs.readFileSync(received.path, 'utf8'), 'Exact fixture bytes');
});

test('dismissed reviews stay quiet until an owner-approved explicit retry', async t => {
  const { a, b, worker, send, decide, prompts } = await fixture(t);
  await worker.enable('claude'); decide({ action: 'cancel' }); const request = await send();
  await until(() => b.requestRow(request.id).background?.state === 'dismissed');
  const count = prompts.length; b.notify(); await a.sync(); await tick(); assert.equal(prompts.length, count);
  assert.equal((await worker.retry(request.id)).changed, false);
  decide({ action: 'approve' }); await worker.retry(request.id);
  await until(() => a.requestRow(request.id).status === 'completed');
});

test('revocation aborts a model in flight, blocks normal-chat double claims, and never sends its late answer', async t => {
  let entered = false, aborted = false;
  const { a, b, worker, send } = await fixture(t, { run: async (_config, _envelope, { signal }) => {
    entered = true; await new Promise(resolve => signal.addEventListener('abort', () => { aborted = true; resolve(); }, { once: true }));
    return { outcome: 'reply', text: 'This late result must not be sent' };
  } });
  await b.policy(a.id, ['message'], []); await worker.enable('codex');
  const request = await send(); await until(() => entered);
  assert.throws(() => b.claim(request.id), { code: 'request_busy' });
  await assert.rejects(b.complete(request.id, 'Competing completion'), { code: 'request_busy' });
  await b.policy(a.id, [], []); await until(() => aborted && !worker.active);
  assert.equal(b.requestRow(request.id).background.state, 'interrupted');
  assert.equal(a.requestRow(request.id).result, undefined);
});

test('remote cancellation and disabling the helper stop execution without granting new permissions', async t => {
  let entered = 0;
  const { a, b, worker, send } = await fixture(t, { run: async (_config, _envelope, { signal }) => {
    entered++; await new Promise(resolve => signal.addEventListener('abort', resolve, { once: true }));
    return { outcome: 'reply', text: 'Must not send' };
  } });
  await b.policy(a.id, ['message'], []); await worker.enable('codex');
  const first = await send(); await until(() => entered === 1); await a.cancel(first.id);
  await until(() => !worker.active); assert.equal(b.requestRow(first.id).status, 'cancelled');
  const second = await send(); await until(() => entered === 2); await worker.disable();
  assert.equal(b.requestRow(second.id).background.state, 'interrupted'); assert.equal(worker.status().enabled, false);
  const third = await send(); await tick(); assert.equal(b.requestRow(third.id).background, undefined);
});

test('only selected published context reaches the model; missing context and external tasks stay for the owner', async t => {
  const { a, b, worker, runs, send } = await fixture(t);
  const selected = await b.publish(a.id, 'Allowed excerpt', 'Allowed data');
  await b.publish(a.id, 'Other excerpt', 'Must not enter this run');
  await b.policy(a.id, ['context', 'task'], [selected.source.id]); await worker.enable('claude');
  const context = await send('context', { source_ids: [selected.source.id] });
  await until(() => a.requestRow(context.id).status === 'completed');
  assert.deepEqual(runs[0].sources.map(s => s.content), ['Allowed data']);
  const task = await send('task', { message: 'Make a bank payment' });
  await until(() => b.requestRow(task.id).background?.state === 'needs_owner');
  assert.equal(b.requestRow(task.id).status, 'working'); assert.equal(a.requestRow(task.id).result, undefined);
  assert.equal(worker.status().jobs.find(j => j.request_id === task.id).draft, 'A scoped fixture reply');
});

test('failed runs and persisted interrupted work are not automatically repeated after restart', async t => {
  let runs = 0;
  const { a, b, worker, runtime, send } = await fixture(t, { run: async () => { runs++; throw new Error('private diagnostic'); } });
  await b.policy(a.id, ['message'], []); await worker.enable('codex');
  const request = await send(); await until(() => b.requestRow(request.id).background?.state === 'error');
  assert.equal(b.requestRow(request.id).background.error, 'background_failed');
  await worker.close(); b.requestRow(request.id).background.state = 'running'; b.store.save();
  const restarted = new BackgroundAgent(b, { runtime });
  try { await tick(); assert.equal(runs, 1); assert.equal(b.requestRow(request.id).background.state, 'interrupted'); }
  finally { await restarted.close(); }
});

test('burst deliveries are serialized and bounded by a durable ten-request hourly cap', async t => {
  let active = 0, maximum = 0, runs = 0;
  const { a, b, worker, send } = await fixture(t, { run: async () => {
    runs++; active++; maximum = Math.max(maximum, active); await tick(); active--; return { outcome: 'reply', text: 'Bounded reply' };
  } });
  await b.policy(a.id, ['message'], []); await worker.enable('codex');
  for (let i = 0; i < 11; i++) await send();
  await until(() => runs === 10 && !worker.active);
  assert.equal(maximum, 1); assert.equal(worker.status().requests_started_last_hour, 10);
  assert.equal(Object.values(b.data.requests).filter(row => !row.background).length, 1);
  await worker.disable(); await worker.enable('codex'); await tick(); assert.equal(runs, 10);
});

test('runtime subprocesses time out, honor cancellation and bound both stdout and stderr', async () => {
  await assert.rejects(runProcess(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { timeout: 30 }), { code: 'runtime_timeout' });
  await assert.rejects(runProcess(process.execPath, ['-e', 'process.stderr.write("x".repeat(2000))'], { limit: 100 }), { code: 'runtime_output_limit' });
  const controller = new AbortController();
  const run = runProcess(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { signal: controller.signal });
  controller.abort(); await assert.rejects(run, { code: 'cancelled' });
  const result = await runProcess(process.execPath, ['-e', 'process.stdin.pipe(process.stdout)'], { input: '`literal`; $(data)\n' });
  assert.equal(result.output, '`literal`; $(data)\n');
});

test('vendor results require a completed turn, bounded structured answer and no unexpected tools', () => {
  const answer = { outcome: 'reply', text: 'A fixture answer' };
  const claude = { subtype: 'success', is_error: false, structured_output: answer };
  assert.deepEqual(parseRuntimeResponse('claude', { code: 0, output: JSON.stringify(claude) }), answer);
  assert.throws(() => parseRuntimeResponse('claude', { code: 1, output: JSON.stringify({ is_error: true, api_error: 'usage_limit_reached', api_error_status: 429 }) }), { code: 'runtime_usage_limit' });
  const events = [{ type: 'item.completed', item: { type: 'agent_message', text: JSON.stringify(answer) } }, { type: 'turn.completed' }];
  const codex = values => ({ code: 0, output: values.map(v => JSON.stringify(v)).join('\n') });
  assert.deepEqual(parseRuntimeResponse('codex', codex(events)), answer);
  assert.throws(() => parseRuntimeResponse('codex', codex(events.slice(0, 1))), { code: 'runtime_invalid_answer' });
  assert.throws(() => parseRuntimeResponse('codex', codex([{ type: 'item.completed', item: { type: 'command_execution' } }, ...events])), { code: 'runtime_invalid_answer' });
  assert.throws(() => parseRuntimeResponse('claude', { code: 0, output: JSON.stringify({ ...claude, structured_output: { ...answer, text: 'x'.repeat(20000) } }) }), { code: 'runtime_invalid_answer' });
});
