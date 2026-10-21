import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import { spawnSync } from 'node:child_process';
import { setImmediate as immediate } from 'node:timers/promises';
import { BASE_TOOLS, DesktopProfile, MAX_INPUT_BYTES, MCPServer, RelayClient, relayOrigin, runStdio, strictJSON, valid } from '../server.mjs';

const decoded = (result) => JSON.parse(result.content[0].text);
const init = async (server, capabilities = {}) => {
  const response = await server.handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities, clientInfo: { name: 'test-host', version: '1' } } });
  await server.handle({ jsonrpc: '2.0', method: 'notifications/initialized' });
  return response;
};
const reply = (server, id, result) => server.handle({ jsonrpc: '2.0', id, result });
async function until(condition) {
  const deadline = Date.now() + 2000;
  while (!condition()) { if (Date.now() > deadline) throw new Error('test did not reach expected state'); await immediate(); }
}
function fixture(options = {}) {
  const messages = []; const calls = [];
  const request = { id: 'r1', recipient_agent_id: 'a1', sender_agent_id: 'a2', kind: 'task', source_ids: [], message: 'Please arrange a meeting.', status: 'pending_approval', updated_at: 10 };
  const state = { profile: { relay: 'https://relay.test', owner_id: 'o1', owner_name: 'Owner', agent_id: 'a1', agent_name: 'Existing agent' } };
  const control = { role: 'agent', ownerId: 'o1', request, consentStatus: 'pending', consentAnswer: { decision: 'approve' }, consentExpiresAt: null };
  const client = (role) => ({ call: async (method, endpoint, body) => {
    calls.push({ role, method, endpoint, body });
    if (endpoint === '/v1/me') return role === 'owner' ? { role: 'owner', id: control.ownerId } : { role: control.role, id: 'a1', owner_id: 'o1' };
    if (endpoint === '/v1/agents') return { agents: [{ id: 'a2', name: 'Remote agent' }] };
    if (endpoint === '/v1/requests' && method === 'GET') return { requests: [structuredClone(request), { id: 'outgoing', recipient_agent_id: 'a2', message: 'Untrusted' }] };
    if (endpoint.endsWith('/sources')) return { sources: [{ id: 's1', kind: 'file', data_base64: 'PRIVATE-FILE' }, { id: 's2', kind: 'context', content: 'Allowed context' }] };
    if (endpoint === '/v1/requests/r1' && method === 'GET') return structuredClone(request);
    if (endpoint === '/v1/consents' && method === 'POST') return { id: 'c1', url: 'https://relay.test/consent/c1', expires_at: control.consentExpiresAt || Date.now() / 1000 + body.expires_in };
    if (endpoint === '/v1/consents/c1/cancel') { control.consentStatus = 'declined'; control.consentAnswer = {}; return { id: 'c1', status: 'declined', content: {} }; }
    if (endpoint === '/v1/consents/c1') return { id: 'c1', status: control.consentStatus, content: control.consentAnswer };
    if (endpoint === '/v1/contact-invites/accept') return { connected: true, owner_id: 'o2' };
    if (endpoint.endsWith('/decision')) return { ...request, status: body.decision === 'approve' ? 'ready' : 'denied' };
    if (endpoint.endsWith('/complete')) return { ...request, status: 'completed', result: body, attribution: { type: role === 'owner' ? 'human' : 'agent', id: role === 'owner' ? 'o1' : 'a1' } };
    return { id: 'r1', status: 'working' };
  } });
  state.agentClient = client('agent'); state.ownerClient = client('owner');
  const profile = { load: () => state, ready: async () => state, contactInvite: () => ({ relay: state.profile.relay, token: 'PRIVATE-CONTACT' }) };
  const server = new MCPServer({ profile, send: (message) => messages.push(message), ...options });
  return { server, calls, messages, control, state, profile };
}
const mutations = (calls) => calls.filter((call) => call.method === 'POST' && !call.endpoint.startsWith('/v1/consents'));
const consentClosures = (calls) => calls.filter((call) => call.endpoint === '/v1/consents/c1/cancel');

test('handshake lists no human tools unless host advertises review', async () => {
  for (const [capabilities, human] of [[{}, false], [{ elicitation: {} }, true], [{ elicitation: { form: {} } }, true], [{ elicitation: { url: {} } }, true], [{ elicitation: { form: false } }, false]]) {
    const { server, calls } = fixture();
    const result = await init(server, capabilities);
    assert.equal(result.result.protocolVersion, '2025-11-25');
    assert.deepEqual(result.result.capabilities, { tools: { listChanged: false } });
    const listed = await server.handle({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
    assert.equal(listed.result.tools.some((item) => item.name === 'review_request'), human);
    assert.equal(calls.length, 0);
    assert.equal(result.result.capabilities.experimental, undefined);
    server.close();
  }
});

test('protocol initializes explicitly and tools/call notifications do not run', async () => {
  const { server, calls } = fixture();
  assert.equal((await server.handle({ jsonrpc: '2.0', id: 2, method: 'tools/list' })).error.code, -32002);
  await init(server);
  assert.equal(await server.handle({ jsonrpc: '2.0', method: 'tools/call', params: { name: 'request', arguments: {} } }), null);
  assert.deepEqual(calls, []);
  for (const id of [true, null, 1.5, [], {}]) assert.equal((await server.handle({ jsonrpc: '2.0', id, method: 'ping' })).error.code, -32600);
});

test('tools reject credential, approval, path and authority arguments before relay access', async () => {
  const { server, calls } = fixture(); await init(server, { elicitation: {} });
  for (const [name, args] of [
    ['ready', { token: 'SECRET' }], ['review_request', { request_id: 'r1', decision: 'approve' }],
    ['connect_contact', { token: 'SECRET' }], ['get_request', { request_id: '../owners' }],
    ['complete_request', { request_id: 'r1', text: 'yes', attribution: { type: 'human' } }],
    ['complete_request', { request_id: 'r1', text: '\ud800' }],
    ['wait_for_request', { timeout_seconds: 46 }],
  ]) assert.equal(decoded(await server.callTool(name, args)).error.code, 'invalid_arguments');
  assert.equal(calls.length, 0);
  for (const tool of BASE_TOOLS) assert.equal(tool.inputSchema.additionalProperties, false);
});

test('agent identity is rechecked before every model operation', async () => {
  const { server, calls, control } = fixture(); await init(server);
  assert.equal((await server.callTool('peers', {})).isError, false);
  control.role = 'owner';
  assert.equal(decoded(await server.callTool('cancel_request', { request_id: 'r1' })).error.code, 'agent_identity_error');
  assert.equal(mutations(calls).length, 0);
});

test('agent tools retain attribution, filter inbox and omit file bytes', async () => {
  const { server, calls } = fixture(); await init(server);
  const sent = await server.callTool('request', { recipient_agent_id: 'a2', kind: 'ask_human', message: 'When?', idempotency_key: 'stable' });
  assert.equal(sent.isError, false);
  assert.deepEqual(mutations(calls)[0].body.source_ids, []);
  assert.equal(mutations(calls)[0].body.idempotency_key, 'stable');
  assert.deepEqual(decoded(await server.callTool('inbox', {})).requests.map((item) => item.id), ['r1']);
  assert.equal(JSON.stringify(await server.callTool('read_sources', { request_id: 'r1' })).includes('PRIVATE-FILE'), false);
  const completed = decoded(await server.callTool('complete_request', { request_id: 'r1', text: 'Done' }));
  assert.equal(completed.attribution.type, 'agent');
});

test('wait returns metadata only and excludes acknowledged incoming requests', async () => {
  const { server } = fixture(); await init(server);
  const incoming = decoded(await server.callTool('wait_for_request', { timeout_seconds: 0 }));
  assert.equal(incoming.status, 'available');
  assert.equal(incoming.requests[0].message, undefined);
  assert.equal(incoming.autonomous_chat_wakeup, false);
  const excluded = decoded(await server.callTool('wait_for_request', { timeout_seconds: 0, exclude_request_ids: ['r1'] }));
  assert.equal(excluded.status, 'timeout');
  assert.equal(decoded(await server.callTool('wait_for_request', { known_status: 'ready' })).error.code, 'invalid_arguments');
});

test('native review displays canonical scope and only correlated human response mutates', async () => {
  const { server, messages, calls } = fixture(); await init(server, { elicitation: { form: {} } });
  const pending = server.callTool('review_request', { request_id: 'r1' });
  await until(() => messages.length > 0);
  const prompt = messages[0];
  assert.equal(prompt.method, 'elicitation/create');
  assert.match(prompt.params.message, /Please arrange a meeting/);
  assert.match(prompt.params.message, /"recipient_agent_id": "a1"/);
  assert.equal(prompt.params.requestedSchema.properties.decision.default, undefined);
  await reply(server, 'unrelated-id', { action: 'accept', content: { decision: 'approve' } });
  assert.equal(mutations(calls).length, 0);
  await reply(server, prompt.id, { action: 'accept', content: { decision: 'approve' } });
  const result = decoded(await pending);
  assert.equal(result.status, 'ready');
  assert.deepEqual(mutations(calls), [{ role: 'owner', method: 'POST', endpoint: '/v1/requests/r1/decision', body: { decision: 'approve' } }]);
  await reply(server, prompt.id, { action: 'accept', content: { decision: 'deny' } });
  assert.equal(mutations(calls).length, 1);
});

test('dismissal and malformed form data never decide a request', async () => {
  for (const response of [{ action: 'decline' }, { action: 'cancel' }, { action: 'accept', content: { decision: 'approve', token: 'SECRET' } }, { action: 'accept', content: {} }, { action: 'accept', content: { text: 'yes' } }]) {
    const { server, calls, messages } = fixture(); await init(server, { elicitation: {} });
    const pending = server.callTool('review_request', { request_id: 'r1' });
    await until(() => messages.length);
    await reply(server, messages[0].id, response);
    const result = await pending;
    assert.equal(mutations(calls).length, 0);
    assert.equal(result.isError, response.action === 'accept');
  }
});

test('owner mismatch, recipient mismatch and changed scope fail closed', async () => {
  for (const mismatch of ['owner', 'recipient', 'changed']) {
    const { server, calls, messages, control } = fixture(); await init(server, { elicitation: {} });
    if (mismatch === 'owner') control.ownerId = 'different-owner';
    if (mismatch === 'recipient') control.request.recipient_agent_id = 'a3';
    const pending = server.callTool('review_request', { request_id: 'r1' });
    if (mismatch === 'changed') {
      await until(() => messages.length); control.request.message = 'Different scope';
      await reply(server, messages[0].id, { action: 'accept', content: { decision: 'approve' } });
    }
    assert.equal((await pending).isError, true);
    assert.equal(mutations(calls).length, 0);
  }
});

test('ask_human uses only exact native human text and owner completion', async () => {
  const { server, calls, messages, control } = fixture(); await init(server, { elicitation: {} });
  control.request.kind = 'ask_human'; control.request.status = 'waiting_human';
  const pending = server.callTool('review_request', { request_id: 'r1' });
  await until(() => messages.length);
  assert.deepEqual(messages[0].params.requestedSchema.required, ['text']);
  await reply(server, messages[0].id, { action: 'accept', content: { text: 'Thursday works for me.' } });
  const result = decoded(await pending);
  assert.equal(result.attribution.type, 'human');
  assert.deepEqual(mutations(calls)[0], { role: 'owner', method: 'POST', endpoint: '/v1/requests/r1/complete', body: { text: 'Thursday works for me.', source_ids: [] } });
});

test('contact token stays private and native decision alone permits acceptance', async () => {
  const { server, calls, messages } = fixture(); await init(server, { elicitation: {} });
  const pending = server.callTool('connect_contact', {}); await until(() => messages.length);
  assert.equal(JSON.stringify(messages).includes('PRIVATE-CONTACT'), false);
  await reply(server, messages[0].id, { action: 'accept', content: { decision: 'connect' } });
  const result = await pending;
  assert.equal(JSON.stringify(result).includes('PRIVATE-CONTACT'), false);
  assert.equal(decoded(result).connected, true);
  assert.equal(mutations(calls)[0].role, 'owner');
});

test('tool cancellation and native timeout notify host and never mutate', async () => {
  for (const cancelled of [false, true]) {
    const { server, messages, calls } = fixture({ elicitationTimeoutMs: 15 }); await init(server, { elicitation: {} });
    const pending = server.handle({ jsonrpc: '2.0', id: 50, method: 'tools/call', params: { name: 'review_request', arguments: { request_id: 'r1' } } });
    await until(() => messages.length);
    if (cancelled) await server.handle({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: 50 } });
    assert.equal((await pending).result.isError, true);
    assert.equal(mutations(calls).length, 0);
    assert.equal(messages.some((message) => message.method === 'notifications/cancelled'), true);
    assert.equal(server.pending.size, 0);
  }
});

test('URL open acceptance never substitutes for submitted browser consent', async () => {
  const { server, calls, messages, control } = fixture({ elicitationTimeoutMs: 2000 }); await init(server, { elicitation: { url: {} } });
  const pending = server.callTool('review_request', { request_id: 'r1' }); await until(() => messages.length);
  assert.equal(messages[0].params.mode, 'url');
  assert.match(messages[0].params.url, /^https:/);
  assert.equal(messages[0].params.requestedSchema, undefined);
  await reply(server, messages[0].id, { action: 'accept', content: { decision: 'approve' } });
  await immediate();
  assert.equal(mutations(calls).length, 0);
  control.consentStatus = 'accepted';
  const result = await pending;
  assert.equal(result.isError, false);
  assert.equal(mutations(calls)[0].endpoint, '/v1/requests/r1/decision');
});

test('URL host refusal leaves request pending, and HTTP URL fallback is forbidden', async () => {
  const first = fixture(); await init(first.server, { elicitation: { url: {} } });
  const pending = first.server.callTool('review_request', { request_id: 'r1' }); await until(() => first.messages.length);
  await reply(first.server, first.messages[0].id, { action: 'decline' });
  assert.equal(decoded(await pending).status, 'unchanged');
  assert.equal(mutations(first.calls).length, 0);
  const second = fixture(); second.state.profile.relay = 'http://127.0.0.1:7340'; await init(second.server, { elicitation: { url: {} } });
  assert.equal(decoded(await second.server.callTool('review_request', { request_id: 'r1' })).error.code, 'human_review_unsupported');
  assert.equal(mutations(second.calls).length, 0);
});

test('URL review refuses redirected origins, bearer URLs and mismatched consent identities', async () => {
  for (const badUrl of ['https://attacker.test/consent/c1', 'https://relay.test/consent/c1?token=PRIVATE', 'https://relay.test/consent/other-id']) {
    const { server, state, messages, calls } = fixture(); await init(server, { elicitation: { url: {} } });
    const original = state.ownerClient.call;
    state.ownerClient.call = async (method, endpoint, ...rest) => endpoint === '/v1/consents' ? { id: 'c1', url: badUrl } : original(method, endpoint, ...rest);
    assert.equal((await server.callTool('review_request', { request_id: 'r1' })).isError, true);
    assert.equal(messages.length, 0); assert.equal(mutations(calls).length, 0);
  }
  const { server, state, calls } = fixture(); await init(server, { elicitation: { url: {} } });
  const original = state.ownerClient.call;
  state.ownerClient.call = async (method, endpoint, ...rest) => endpoint === '/v1/consents/c1' ? { id: 'another-consent', status: 'accepted', content: { decision: 'approve' } } : original(method, endpoint, ...rest);
  assert.equal((await server.callTool('review_request', { request_id: 'r1' })).isError, true);
  assert.equal(mutations(calls).length, 0);
});

test('transport rejects unsafe origins, redirects, content types and oversized bodies', async () => {
  for (const value of ['http://example.com', 'http://127.1', 'http://2130706433', 'https://user:pass@example.com', 'https://example.com/api', 'https://example.com?token=secret', 'https://example.com/#fragment', 'https://example.com\\evil']) assert.throws(() => relayOrigin(value));
  for (const value of ['https://relay.test', 'http://localhost:7340', 'http://127.0.0.1:7340', 'http://[::1]:7340']) assert.equal(typeof relayOrigin(value), 'string');
  for (const response of [new Response('', { status: 302, headers: { Location: 'https://attacker.test' } }), new Response('SECRET', { status: 403 }), new Response('SECRET', { status: 200, headers: { 'content-type': 'text/plain' } }), new Response('x'.repeat(8 * 1024 * 1024 + 1), { headers: { 'content-type': 'application/json' } })]) {
    let count = 0;
    const client = new RelayClient('https://relay.test', 'PRIVATE-TOKEN', async (_url, options) => { count++; assert.equal(options.redirect, 'manual'); return response; });
    await assert.rejects(client.call('GET', '/v1/me'), (error) => !String(error).includes('SECRET') && !String(error).includes('PRIVATE-TOKEN'));
    assert.equal(count, 1);
  }
});

test('strict JSON rejects duplicate keys, nonfinite values and deep structures', () => {
  for (const text of ['{"id":1,"id":2}', '{"x":{"a":1,"a":2}}', '{"id":1e999}', '{"x":NaN}', '[1,]', '{"x":1,}', '['.repeat(70) + '0' + ']'.repeat(70)]) assert.throws(() => strictJSON(text));
  assert.equal(strictJSON('{"unicode":"é","nested":[true,null,1.5]}').unicode, 'é');
  assert.equal(valid('é'.repeat(100), { type: 'string', maxLength: 100 }), false);
});

test('stdio is JSON only and bounds partial malicious input', async () => {
  const { profile } = fixture(); const input = new PassThrough(); const output = new PassThrough(); const lines = [];
  output.on('data', (data) => lines.push(...data.toString().trim().split('\n')));
  runStdio(input, output, profile);
  input.write('{"jsonrpc":"2.0","id":7,"method":"ping"}\n');
  input.write('{"id":1,"id":2}\n');
  input.write(Buffer.alloc(MAX_INPUT_BYTES + 1, 120));
  await immediate();
  const results = lines.map(JSON.parse);
  assert.equal(results.find((item) => item.id === 7).jsonrpc, '2.0');
  assert.equal(results.filter((item) => item.error?.code === -32700).length, 2);
});

function privateTest(t) {
  const directory = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'airshower-node-test-')); fs.chmodSync(directory, 0o700);
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, write: (name, value) => fs.writeFileSync(path.join(directory, name), JSON.stringify(value), { mode: 0o600 }) };
}
function identities() { return { owner: { id: 'owner-1', name: 'Owner', token: 'PRIVATE-OWNER' }, agent: { id: 'agent-1', owner_id: 'owner-1', name: 'Claude Desktop', token: 'PRIVATE-AGENT' } }; }
function existingProfile(fixture) {
  const { owner, agent } = identities();
  fixture.write('owner.json', owner); fixture.write('agent.json', agent);
  fixture.write('profile.json', { schema_version: 1, mode: 'remote', relay: 'https://relay.test', owner_id: owner.id, owner_name: owner.name, agent_id: agent.id, agent_name: agent.name });
}

test('private profile interoperates with Python shape and rejects links or permissive files', (t) => {
  const files = privateTest(t); existingProfile(files);
  const profile = new DesktopProfile({ AIRSHOWER_PROFILE: files.directory });
  assert.equal(profile.load().profile.agent_id, 'agent-1');
  const ownerFile = path.join(files.directory, 'owner.json'); fs.chmodSync(ownerFile, 0o644);
  assert.throws(() => profile.load()); fs.chmodSync(ownerFile, 0o600);
  fs.linkSync(ownerFile, path.join(files.directory, 'second-link')); assert.throws(() => profile.load());
  fs.unlinkSync(path.join(files.directory, 'second-link'));
  const link = path.join(files.directory, 'linked'); fs.symlinkSync(files.directory, link);
  assert.throws(() => new DesktopProfile({ AIRSHOWER_PROFILE: link }).load());
});

test('no invitation returns incomplete and does not provision invented identity', async (t) => {
  const files = privateTest(t); let calls = 0;
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: files.directory }, async () => { calls++; });
  await assert.rejects(profile.ready(), (error) => error.code === 'needs_invitation');
  assert.equal(calls, 0); assert.deepEqual(fs.readdirSync(files.directory), []);
});

test('enrollment saves private credentials once and returns no secret from ready', async (t) => {
  const files = privateTest(t); const issued = identities(); let enrollmentCalls = 0;
  const fetcher = async (url, options) => {
    const result = url.endsWith('/v1/enroll') ? (enrollmentCalls++, issued) : { role: 'agent', id: issued.agent.id, owner_id: issued.owner.id };
    if (url.endsWith('/v1/enroll')) assert.equal(options.headers.Authorization, 'Bearer PRIVATE-INVITATION');
    return new Response(JSON.stringify(result), { headers: { 'content-type': 'application/json' } });
  };
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_INVITATION: JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE-INVITATION' }) }, fetcher);
  const server = new MCPServer({ profile }); await init(server);
  for (let n = 0; n < 2; n++) {
    const result = await server.callTool('ready', {}); assert.equal(decoded(result).status, 'relay_connected');
    assert.equal(JSON.stringify(result).includes('PRIVATE-'), false);
    assert.equal(decoded(result).autonomous_chat_wakeup, false);
  }
  assert.equal(enrollmentCalls, 1);
  for (const file of fs.readdirSync(files.directory)) assert.equal(fs.statSync(path.join(files.directory, file)).mode & 0o777, 0o600);
});

test('ambiguous enrollment is journaled and never blindly resubmitted', async (t) => {
  const files = privateTest(t); let calls = 0;
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_INVITATION: JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE' }) }, async () => { calls++; throw new Error('PRIVATE-TOKEN'); });
  await assert.rejects(profile.ready(), (error) => error.code === 'connection_error' && !String(error).includes('PRIVATE'));
  await assert.rejects(profile.ready(), (error) => error.code === 'enrollment_unknown');
  assert.equal(calls, 1);
});

test('cached enrollment is pinned to original relay before transmitting any credential', async (t) => {
  const files = privateTest(t); files.write('enrollment-result.json', identities());
  files.write('enrollment-pending.json', { relay: 'https://original.test', name: 'Owner', agent_name: 'Claude Desktop' });
  let calls = 0;
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_INVITATION: JSON.stringify({ relay: 'https://different.test', token: 'OTHER' }) }, async () => { calls++; });
  await assert.rejects(profile.ready(), (error) => error.code === 'profile_error');
  assert.equal(calls, 0); assert.equal(fs.existsSync(path.join(files.directory, 'profile.json')), false);
});

test('new sensitive contact setting supersedes an old consumed invite', (t) => {
  const files = privateTest(t); existingProfile(files);
  files.write('contact-invite.json', { relay: 'https://relay.test', token: 'OLD' });
  const profile = new DesktopProfile({ AIRSHOWER_PROFILE: files.directory, AIRSHOWER_CONTACT_INVITATION: JSON.stringify({ relay: 'https://relay.test', token: 'NEW' }) });
  assert.equal(profile.contactInvite(profile.load().profile).token, 'NEW');
});

test('user-selected invitation file provisions internally without exposing its credentials', async (t) => {
  const files = privateTest(t); const selected = path.join(files.directory, 'selected-invitation.json');
  fs.writeFileSync(selected, JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE-SELECTED-INVITE' }), { mode: 0o600 });
  const stateDir = path.join(files.directory, 'profile'); const issued = identities(); let calls = 0;
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: stateDir }, async (url, options) => {
    calls++;
    const result = url.endsWith('/enroll') ? issued : { role: 'agent', id: issued.agent.id, owner_id: issued.owner.id };
    if (url.endsWith('/enroll')) assert.equal(options.headers.Authorization, 'Bearer PRIVATE-SELECTED-INVITE');
    return new Response(JSON.stringify(result), { headers: { 'content-type': 'application/json' } });
  });
  const server = new MCPServer({ profile }); await init(server);
  const result = await server.callTool('ready', { invitation_file: selected, name: 'Owner', relay: 'https://relay.test' });
  assert.equal(decoded(result).status, 'relay_connected');
  assert.equal(JSON.stringify(result).includes('PRIVATE-'), false);
  assert.equal(fs.statSync(selected).mode & 0o777, 0o600);
  assert.equal(calls, 2);
  const mismatched = await server.callTool('ready', { invitation_file: selected, name: 'Different' });
  assert.equal(mismatched.isError, true); assert.equal(calls, 2);
});

test('selected invitation rejects malformed, missing, linked, large or conflicting files without leaking', async (t) => {
  const files = privateTest(t); let calls = 0;
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: path.join(files.directory, 'profile') }, async () => { calls++; });
  const server = new MCPServer({ profile }); await init(server);
  for (const [filename, content] of [['malformed', 'PRIVATE-INVALID-CONTENT'], ['large', 'PRIVATE-'.repeat(3000)], ['valid', JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE-INVITE' })]]) fs.writeFileSync(path.join(files.directory, filename), content, { mode: 0o600 });
  fs.symlinkSync(path.join(files.directory, 'valid'), path.join(files.directory, 'link'));
  for (const filename of ['malformed', 'large', 'missing', 'link']) {
    const result = await server.callTool('ready', { invitation_file: path.join(files.directory, filename), name: 'Owner' });
    assert.equal(result.isError, true); assert.equal(JSON.stringify(result).includes('PRIVATE-'), false);
  }
  assert.equal((await server.callTool('ready', { invitation_file: path.join(files.directory, 'valid'), name: 'Owner', relay: 'https://different.test' })).isError, true);
  assert.equal((await server.callTool('ready', { name: 'Owner' })).isError, true);
  assert.equal(calls, 0);
});

test('a user-selected FIFO is rejected without blocking the desktop adapter', (t) => {
  if (process.platform === 'win32') { t.skip('Named pipe fixture is Unix only'); return; }
  const files = privateTest(t); const pipe = path.join(files.directory, 'invitation-pipe');
  const made = spawnSync('mkfifo', [pipe]);
  if (made.error?.code === 'ENOENT') { t.skip('mkfifo fixture utility unavailable'); return; }
  assert.equal(made.status, 0);
  const script = `import { DesktopProfile } from ${JSON.stringify(new URL('../server.mjs', import.meta.url).href)};
    const profile = new DesktopProfile({AIRSHOWER_STATE_DIR:${JSON.stringify(path.join(files.directory, 'profile'))}});
    try { await profile.readyFromFile({invitation_file:${JSON.stringify(pipe)}, name:'Owner'}); process.exitCode=1; }
    catch (error) { if (error.code !== 'invitation_file_error') process.exitCode=2; }`;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], { timeout: 2000, encoding: 'utf8' });
  assert.equal(result.error, undefined); assert.equal(result.status, 0);
});

test('approval password goes only to enrollment and is never stored or returned', async (t) => {
  const files = privateTest(t); const issued = identities(); const secret = 'PRIVATE-APPROVAL-PASSWORD';
  const environment = { AIRSHOWER_STATE_DIR: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_APPROVAL_PASSWORD: secret, AIRSHOWER_INVITATION: JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE-INVITE' }) };
  const profile = new DesktopProfile(environment, async (url, options) => {
    if (url.endsWith('/enroll')) assert.equal(JSON.parse(options.body).consent_secret, secret);
    return new Response(JSON.stringify(url.endsWith('/enroll') ? issued : { role: 'agent', id: issued.agent.id, owner_id: issued.owner.id }), { headers: { 'content-type': 'application/json' } });
  });
  const server = new MCPServer({ profile }); await init(server, { elicitation: { url: {} } });
  const result = await server.callTool('ready', {});
  assert.equal(result.isError, false); assert.equal(JSON.stringify(result).includes(secret), false);
  for (const file of fs.readdirSync(files.directory)) assert.equal(fs.readFileSync(path.join(files.directory, file), 'utf8').includes(secret), false);
  assert.equal(environment.AIRSHOWER_APPROVAL_PASSWORD, undefined);
});

test('invalid approval password does not leave an ambiguous enrollment journal', async (t) => {
  const files = privateTest(t); let called = false;
  const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_APPROVAL_PASSWORD: 'short', AIRSHOWER_INVITATION: JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE' }) }, async () => { called = true; });
  await assert.rejects(profile.ready(), (error) => error.code === 'configuration_error');
  assert.equal(called, false); assert.equal(fs.existsSync(path.join(files.directory, 'enrollment-pending.json')), false);
});

test('URL human answer preserves attribution and rejects answer-shaped decisions', async () => {
  for (const askHuman of [true, false]) {
    const { server, control, calls } = fixture(); await init(server, { elicitation: { url: {} } });
    if (askHuman) { control.request.kind = 'ask_human'; control.request.status = 'waiting_human'; }
    control.consentStatus = 'accepted'; control.consentAnswer = { answer: 'Thursday works.' };
    const result = await server.callTool('review_request', { request_id: 'r1' });
    assert.equal(result.isError, !askHuman);
    if (askHuman) assert.equal(decoded(result).attribution.type, 'human');
    else assert.equal(mutations(calls).length, 0);
  }
});

test('existing profile rejects a changed configured owner before authentication', async (t) => {
  const files = privateTest(t); existingProfile(files); let calls = 0;
  const original = fs.readFileSync(path.join(files.directory, 'profile.json'), 'utf8');
  const profile = new DesktopProfile({ AIRSHOWER_PROFILE: files.directory, AIRSHOWER_NAME: 'Different owner' }, async () => { calls++; });
  await assert.rejects(profile.ready(), (error) => error.code === 'profile_error');
  assert.throws(() => profile.load(), (error) => error.code === 'profile_error');
  assert.equal(calls, 0); assert.equal(fs.readFileSync(path.join(files.directory, 'profile.json'), 'utf8'), original);
});

test('existing enrollment accepts its original invitation and rejects a replacement identity', async (t) => {
  const files = privateTest(t); const issued = identities(); let enrollments = 0;
  const originalInvite = { relay: 'https://relay.test', token: 'ORIGINAL-PRIVATE-INVITE' };
  const environment = { AIRSHOWER_STATE_DIR: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_INVITATION: JSON.stringify(originalInvite) };
  const profile = new DesktopProfile(environment, async () => { enrollments++; return new Response(JSON.stringify(issued), { headers: { 'content-type': 'application/json' } }); });
  await profile.ready(); await profile.ready();
  const originals = Object.fromEntries(fs.readdirSync(files.directory).map((file) => [file, fs.readFileSync(path.join(files.directory, file), 'utf8')]));
  for (const changed of [{ ...originalInvite, token: 'NEW-PRIVATE-INVITE' }, { ...originalInvite, relay: 'https://another.test' }]) {
    environment.AIRSHOWER_INVITATION = JSON.stringify(changed);
    await assert.rejects(profile.ready(), (error) => error.code === 'profile_error' && !String(error).includes('PRIVATE-INVITE'));
  }
  environment.AIRSHOWER_INVITATION = JSON.stringify({ ...originalInvite, contact_invite: 'NEW-CONTACT' });
  await profile.ready();
  assert.equal(profile.contactInvite(profile.load().profile).token, 'NEW-CONTACT');
  assert.equal(enrollments, 1);
  for (const [file, contents] of Object.entries(originals)) assert.equal(fs.readFileSync(path.join(files.directory, file), 'utf8'), contents);
});

test('a legacy profile cannot silently consume or import an unverifiable invitation', async (t) => {
  const files = privateTest(t); existingProfile(files); let called = false;
  const environment = { AIRSHOWER_PROFILE: files.directory, AIRSHOWER_NAME: 'Owner', AIRSHOWER_INVITATION: JSON.stringify({ relay: 'https://relay.test', token: 'UNKNOWN-PRIVATE-INVITE' }) };
  const profile = new DesktopProfile(environment, async () => { called = true; });
  await assert.rejects(profile.ready(), (error) => error.code === 'profile_error');
  delete environment.AIRSHOWER_INVITATION;
  assert.equal((await profile.ready()).profile.owner_name, 'Owner');
  assert.equal(called, false);
});

test('file setup cannot override live extension settings and leave later tools unusable', async (t) => {
  const files = privateTest(t); const selected = path.join(files.directory, 'invite.json');
  fs.writeFileSync(selected, JSON.stringify({ relay: 'https://relay.test', token: 'PRIVATE-INVITE' }), { mode: 0o600 });
  for (const settings of [{ AIRSHOWER_NAME: 'Old name' }, { AIRSHOWER_RELAY: 'https://old-relay.test' }]) {
    let called = false; const directory = path.join(files.directory, 'new-profile');
    const profile = new DesktopProfile({ AIRSHOWER_STATE_DIR: directory, ...settings }, async () => { called = true; });
    await assert.rejects(profile.readyFromFile({ invitation_file: selected, name: 'New name', relay: 'https://relay.test' }), (error) => error.code === 'configuration_error');
    assert.equal(called, false); assert.equal(fs.existsSync(directory), false);
  }
});

test('URL refusal, timeout, errors and abort close their owner-scoped broker sessions', async () => {
  for (const ending of ['decline', 'cancel', 'timeout', 'abort', 'host-error', 'invalid-content']) {
    const { server, calls, messages, control, state } = fixture({ elicitationTimeoutMs: 25 });
    await init(server, { elicitation: { url: {} } });
    const original = state.ownerClient.call; let closeSignal; let closeTimeout;
    state.ownerClient.call = async (method, endpoint, body, signal, timeout) => {
      if (endpoint.endsWith('/cancel')) { closeSignal = signal; closeTimeout = timeout; }
      return original(method, endpoint, body, signal, timeout);
    };
    if (ending === 'invalid-content') { control.consentStatus = 'accepted'; control.consentAnswer = { decision: 'forged' }; }
    const controller = new AbortController();
    const pending = server.callTool('review_request', { request_id: 'r1' }, controller.signal);
    await until(() => messages.some((message) => message.method === 'elicitation/create'));
    const prompt = messages.find((message) => message.method === 'elicitation/create');
    if (ending === 'decline' || ending === 'cancel') await reply(server, prompt.id, { action: ending });
    if (ending === 'abort') controller.abort();
    if (ending === 'host-error') await server.handle({ jsonrpc: '2.0', id: prompt.id, error: { code: -1, message: 'Private host error' } });
    const result = await pending;
    assert.equal(result.isError, !['decline', 'cancel'].includes(ending));
    assert.equal(consentClosures(calls).length, 1, ending);
    assert.equal(consentClosures(calls)[0].role, 'owner');
    assert.equal(closeSignal, undefined); assert.equal(closeTimeout, 3000);
    assert.equal(control.consentStatus, 'declined');
    assert.equal(mutations(calls).length, 0);
    assert.equal(server.pending.size, 0);
  }
});

test('URL approval is closed if scope changes or the protected owner operation fails', async () => {
  for (const reason of ['scope-changed', 'owner-operation-failed']) {
    const { server, control, state, calls } = fixture(); await init(server, { elicitation: { url: {} } });
    control.consentStatus = 'accepted'; let reads = 0; const original = state.ownerClient.call;
    state.ownerClient.call = async (method, endpoint, ...args) => {
      if (endpoint === '/v1/requests/r1' && method === 'GET' && ++reads === 2 && reason === 'scope-changed') control.request.message = 'Changed scope';
      if (endpoint.endsWith('/decision') && reason === 'owner-operation-failed') throw new Error('PRIVATE-FAILURE');
      return original(method, endpoint, ...args);
    };
    const result = await server.callTool('review_request', { request_id: 'r1' });
    assert.equal(result.isError, true); assert.equal(JSON.stringify(result).includes('PRIVATE-FAILURE'), false);
    assert.equal(consentClosures(calls).length, 1);
    assert.equal(control.consentStatus, 'declined');
  }
});

test('URL successful protected operation keeps its accepted audit result', async () => {
  const { server, control, calls } = fixture(); await init(server, { elicitation: { url: {} } });
  control.consentStatus = 'accepted';
  assert.equal((await server.callTool('review_request', { request_id: 'r1' })).isError, false);
  assert.equal(consentClosures(calls).length, 0); assert.equal(control.consentStatus, 'accepted');
  const created = calls.find((call) => call.endpoint === '/v1/consents');
  assert.equal(created.body.expires_in, 45);
});

test('URL browser deadline bounds the host prompt and closed contact flow', async () => {
  const { server, control, calls, messages } = fixture({ elicitationTimeoutMs: 1000 });
  await init(server, { elicitation: { url: {} } });
  control.consentExpiresAt = (Date.now() + 25) / 1000;
  const start = Date.now();
  const pending = server.callTool('connect_contact', {});
  await until(() => messages.length);
  await reply(server, messages[0].id, { action: 'accept' });
  const result = await pending;
  assert.equal(result.isError, true); assert.ok(Date.now() - start < 500);
  assert.equal(consentClosures(calls).length, 1); assert.equal(mutations(calls).length, 0);
});

test('URL cleanup failure is reported without pretending browser review is closed', async () => {
  const { server, state, messages, calls } = fixture(); await init(server, { elicitation: { url: {} } });
  const original = state.ownerClient.call;
  state.ownerClient.call = async (method, endpoint, ...args) => {
    if (endpoint.endsWith('/cancel')) throw new Error('PRIVATE-TRANSPORT-DETAIL');
    return original(method, endpoint, ...args);
  };
  const pending = server.callTool('review_request', { request_id: 'r1' }); await until(() => messages.length);
  await reply(server, messages[0].id, { action: 'decline' });
  const result = await pending;
  assert.equal(decoded(result).error.code, 'consent_close_unconfirmed');
  assert.equal(JSON.stringify(result).includes('PRIVATE'), false); assert.equal(mutations(calls).length, 0);
});
