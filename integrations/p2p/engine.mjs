import fs from 'node:fs';
import path from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { SafeError } from '../desktop/server.mjs';
import { PrivateStore } from './storage.mjs';
import { PeerTransport } from './transport.mjs';

const KINDS = ['message', 'ask_human', 'context', 'file', 'task'];
const STATES = ['pending_approval', 'waiting_human', 'ready', 'working', 'completed', 'denied', 'cancelled', 'expired'];
const TERMINAL = ['completed', 'denied', 'cancelled', 'expired'];
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const KEY = /^[a-f0-9]{64}$/;
const canonicalKeys = ['id', 'sender_agent_id', 'recipient_agent_id', 'kind', 'message', 'source_ids', 'idempotency_key', 'created_at', 'expires_at'];
const fail = (code, message) => { throw new SafeError(code, message); };
const copy = value => JSON.parse(JSON.stringify(value));
const hash = value => createHash('sha256').update(value).digest('hex');
const text = (value, size = 16384, empty = false) => typeof value === 'string' && (empty || value.trim()) && Buffer.byteLength(JSON.stringify(value)) <= size + 2 && !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(value);
const ids = value => Array.isArray(value) && value.length <= 30 && new Set(value).size === value.length && value.every(v => typeof v === 'string' && UUID.test(v));
const fields = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const canonical = row => Object.fromEntries(canonicalKeys.map(key => [key, row[key]]));
const same = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
const safeFilename = value => {
  let result = '';
  for (const character of value.replace(/[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/gu, '_')) {
    if (Buffer.byteLength(JSON.stringify(result + character)) > 257) break;
    result += character;
  }
  return result || 'file';
};

export class PeerEngine {
  constructor({ directory, name, mode = 'internet', bindAddr, timeout = 8000, retryMs = 2000, approve }) {
    this.options = { mode, bindAddr, timeout }; this.directory = directory; this.name = name;
    this.retryMs = retryMs; this.approve = approve; this.syncing = false; this.reviews = new Set(); this.closed = false;
  }
  async start() {
    if (this.starting) return this.starting;
    this.starting = (async () => {
      this.closed = false;
      this.store = await PrivateStore.open(this.directory); this.data = this.store.data;
      if (!this.data.name) {
        if (!text(this.name, 160) || /[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/u.test(this.name)) fail('needs_name', 'Choose a display name in Airshower settings or the ready tool.');
        this.data.name = this.name; this.store.save();
      } else if (this.name && this.name !== this.data.name) fail('identity_mismatch', 'The configured name differs from this existing identity. Existing data was preserved.');
      this.transport = await new PeerTransport({ secret: this.data.secret, authorize: id => Boolean(this.data.contacts[id]?.active), receive: (id, value) => this.receive(id, value), ...this.options }).start();
      this.id = this.transport.id;
      this.timer = setInterval(() => { this.sync().catch(() => {}); }, this.retryMs); this.timer.unref();
      return this;
    })();
    try { return await this.starting; }
    catch (error) { await this.close(); this.starting = null; throw error; }
  }
  status() {
    this.store.assertHealthy();
    return { status: 'p2p_ready', endpoint_id: this.id, owner_name: this.data.name, transport: 'iroh', encrypted_between_peers: true,
      account_server_required: false, autonomous_chat_wakeup: false, background_service_installed: false,
      contacts: Object.values(this.data.contacts).filter(c => c.active).length, relay_address_available: Boolean(this.transport.endpoint.addr().relayUrl()),
      connection_paths: Object.fromEntries(this.transport.paths), queued: Object.values(this.data.requests).filter(r => r.sender_agent_id === this.id && !r.delivered && !TERMINAL.includes(r.status)).length };
  }
  contact(id) { this.store.assertHealthy(); const result = this.data.contacts[id]; if (!result?.active) fail('not_connected', 'This contact has not been approved locally or has been disconnected.'); return result; }
  requestRow(id) { const row = this.data.requests[id]; if (!row) fail('not_found', 'The request is unavailable.'); this.expire(row); return row; }
  incoming(id) { const row = this.requestRow(id); if (row.recipient_agent_id !== this.id) fail('wrong_recipient', 'Only this request’s recipient can handle it.'); this.contact(row.sender_agent_id); return row; }
  public(row) { return copy({ ...canonical(row), status: row.status, revision: row.revision, updated_at: row.updated_at, delivered: Boolean(row.delivered), ...(row.result ? { result: row.result, attribution: row.attribution } : {}), ...(row.cancel_requested ? { cancellation_pending: true } : {}) }); }
  snapshot(row) { const value = this.public(row); delete value.delivered; delete value.cancellation_pending; return value; }
  changed(row) { row.revision++; row.updated_at = Date.now(); row.dirty = true; this.store.save(); }
  expire(row) {
    if (row.expires_at <= Date.now() && !TERMINAL.includes(row.status) && (row.recipient_agent_id === this.id || !row.delivered)) {
      row.status = 'expired'; this.changed(row);
    }
  }
  sourcesFor(row) {
    const sources = row.source_ids.map(id => this.data.sources[id]);
    if (sources.some(s => !s || !s.active || s.peer_id !== row.sender_agent_id || s.kind !== row.kind)) fail('source_denied', 'A selected source is unavailable for this contact.');
    return sources;
  }
  permitted(row) {
    this.contact(row.sender_agent_id); this.expire(row);
    if (!['ready', 'working'].includes(row.status)) fail('approval_required', 'This request is not currently authorized for processing.');
    this.sourcesFor(row);
    if (row.basis === 'policy') {
      const policy = this.contact(row.sender_agent_id).policy;
      if (!policy?.kinds.includes(row.kind) || !row.source_ids.every(id => policy.source_ids.includes(id))) fail('approval_required', 'The standing permission was changed or revoked.');
    }
  }
  async human(prompt, signal) {
    if (!this.approve) fail('human_review_unavailable', 'A supported native owner prompt is required. No decision was made.');
    if (signal?.aborted) fail('cancelled', 'The review was cancelled.');
    const answer = await this.approve(prompt, signal);
    if (signal?.aborted) fail('cancelled', 'The review was cancelled.');
    if (!answer || typeof answer !== 'object') fail('invalid_human_response', 'The owner prompt returned no valid decision.');
    return answer;
  }
  async connect(cardInput, signal) {
    let card; try { card = this.transport.parseCard(cardInput); } catch { fail('invalid_card', 'Use the complete public Airshower connection card from the intended person.'); }
    const answer = await this.human({ kind: 'confirm', title: 'Connect with ' + card.name + '?', detail: 'Verify this card came from the intended person. The name is supplied by them.\n\nIdentity: ' + card.endpoint_id + '\n\nConnecting permits requests. It shares no files and approves no actions. Both people must connect each other’s cards.', accept: 'Connect' }, signal);
    if (answer.action !== 'approve') return { connected: false };
    if (!this.data.contacts[card.endpoint_id] && Object.keys(this.data.contacts).length >= 50) fail('capacity', 'This preview supports up to 50 contacts.');
    const previous = this.data.contacts[card.endpoint_id];
    this.data.contacts[card.endpoint_id] = { card, active: true, policy: previous?.policy || { kinds: [], source_ids: [] } };
    this.store.save(); await this.sync(); return { connected: true, endpoint_id: card.endpoint_id, name: card.name, note: 'The other owner must also connect your public card.' };
  }
  async disconnect(id, signal) {
    const contact = this.contact(id);
    const answer = await this.human({ kind: 'confirm', title: 'Disconnect ' + contact.card.name + '?', detail: 'Block this peer from future requests, source reads, and downloads on this device. Previously received information cannot be retracted.', accept: 'Disconnect' }, signal);
    if (answer.action !== 'approve') return { changed: false };
    contact.active = false; contact.policy = { kinds: [], source_ids: [] };
    for (const source of Object.values(this.data.sources)) if (source.peer_id === id) source.active = false;
    for (const row of Object.values(this.data.requests)) {
      if (row.sender_agent_id === id && row.recipient_agent_id === this.id) {
        row.access_revoked = true;
        if (!TERMINAL.includes(row.status)) { row.status = 'denied'; row.revision++; row.updated_at = Date.now(); row.dirty = true; }
      } else if (row.recipient_agent_id === id && row.sender_agent_id === this.id && !TERMINAL.includes(row.status)) row.cancel_requested = true;
    }
    this.store.save(); this.transport.disconnect(id); return { changed: true };
  }
  async policy(id, kinds, sourceIds, signal) {
    const contact = this.contact(id);
    if (!Array.isArray(kinds) || new Set(kinds).size !== kinds.length || kinds.some(k => !KINDS.includes(k) || k === 'ask_human') || !ids(sourceIds)) fail('invalid_arguments', 'Choose exact request kinds and published source IDs. Human questions always require a human answer.');
    if (sourceIds.some(id => !this.data.sources[id]?.active || this.data.sources[id].peer_id !== contact.card.endpoint_id)) fail('source_denied', 'The selected source is not published to this contact.');
    const answer = await this.human({ kind: 'confirm', title: 'Change permissions for ' + contact.card.name + '?', detail: 'Allow your agent to handle these request kinds without asking each time: ' + (kinds.join(', ') || 'none') + '.\n\nExact sources: ' + (sourceIds.map(id => this.data.sources[id].label).join(', ') || 'none') + '.\n\nThis does not grant access to chats, other files, money, calendars, or other applications.', accept: 'Save permissions' }, signal);
    if (answer.action !== 'approve') return { changed: false };
    this.contact(id); contact.policy = { kinds, source_ids: sourceIds }; this.store.save(); return { changed: true };
  }
  async send(args) {
    const contact = this.contact(args.recipient_agent_id); const sourceIds = args.source_ids || [];
    if (!KINDS.includes(args.kind) || !text(args.message) || !text(args.idempotency_key, 128) || !ids(sourceIds) || (['context', 'file'].includes(args.kind) ? !sourceIds.length : sourceIds.length)) fail('invalid_arguments', 'Invalid request fields or source scope.');
    const existing = Object.values(this.data.requests).find(r => r.sender_agent_id === this.id && r.idempotency_key === args.idempotency_key);
    if (existing) {
      if (existing.recipient_agent_id !== args.recipient_agent_id || existing.kind !== args.kind || existing.message !== args.message || JSON.stringify(existing.source_ids) !== JSON.stringify(sourceIds)) fail('idempotency_conflict', 'This idempotency key already belongs to a different request.');
      await this.sync(); return this.public(existing);
    }
    if (Object.keys(this.data.requests).length >= 256) fail('capacity', 'This preview profile has reached its request capacity.');
    const now = Date.now(); const row = { id: randomUUID(), sender_agent_id: this.id, recipient_agent_id: contact.card.endpoint_id, kind: args.kind, message: args.message, source_ids: sourceIds, idempotency_key: args.idempotency_key, created_at: now, expires_at: now + 86400000, updated_at: now, status: 'queued', revision: 0, delivered: false };
    this.data.requests[row.id] = row; this.store.save(); await this.sync(); return this.public(row);
  }
  receive(peerId, message) {
    this.contact(peerId);
    if (!message || message.version !== 1 || typeof message.type !== 'string') fail('invalid_request', 'Invalid peer envelope.');
    if (message.type === 'submit' && fields(message, ['version', 'type', 'request'])) {
      const row = message.request;
      if (!fields(row, canonicalKeys) || !UUID.test(row.id) || row.sender_agent_id !== peerId || row.recipient_agent_id !== this.id || !KINDS.includes(row.kind) || !text(row.message) || !text(row.idempotency_key, 128) || !ids(row.source_ids) || (['file', 'context'].includes(row.kind) ? !row.source_ids.length : row.source_ids.length) || !Number.isSafeInteger(row.created_at) || row.created_at > Date.now() + 300000 || !Number.isSafeInteger(row.expires_at) || row.expires_at <= row.created_at || row.expires_at > row.created_at + 86400000) fail('invalid_request', 'Invalid request scope.');
      const old = this.data.requests[row.id] || Object.values(this.data.requests).find(r => r.sender_agent_id === peerId && r.idempotency_key === row.idempotency_key);
      if (old) { if (!same(old, row)) fail('idempotency_conflict', 'Request changed on replay.'); this.expire(old); return this.snapshot(old); }
      if (Object.keys(this.data.requests).length >= 256) fail('capacity', 'Request capacity reached.');
      this.sourcesFor(row);
      const policy = this.contact(peerId).policy;
      const automatic = row.kind !== 'ask_human' && policy.kinds.includes(row.kind) && row.source_ids.every(id => policy.source_ids.includes(id));
      const saved = { ...copy(row), status: row.expires_at <= Date.now() ? 'expired' : row.kind === 'ask_human' ? 'waiting_human' : automatic ? 'ready' : 'pending_approval', revision: 1, updated_at: Date.now(), delivered: true, basis: automatic ? 'policy' : 'none', dirty: true };
      this.data.requests[row.id] = saved; this.store.save(); return this.snapshot(saved);
    }
    if (['get', 'cancel', 'file'].includes(message.type) && fields(message, message.type === 'file' ? ['version', 'type', 'request_id', 'source_id'] : ['version', 'type', 'request_id'])) {
      const row = this.incoming(message.request_id);
      if (row.sender_agent_id !== peerId) fail('not_found', 'Unavailable request.');
      if (message.type === 'cancel' && !TERMINAL.includes(row.status)) { row.status = 'cancelled'; this.changed(row); }
      if (message.type === 'file') {
        if (row.access_revoked || row.status !== 'completed' || row.kind !== 'file' || !row.result.source_ids.includes(message.source_id)) fail('source_denied', 'File is not part of a currently permitted completed handoff.');
        if (row.basis === 'policy') {
          const policy = this.contact(peerId).policy;
          if (!policy.kinds.includes('file') || !row.source_ids.every(id => policy.source_ids.includes(id))) fail('source_denied', 'The standing file permission was revoked.');
        }
        const source = this.sourcesFor(row).find(s => s.id === message.source_id);
        return { id: source.id, filename: source.filename, sha256: source.sha256, size: source.size, data_base64: source.data_base64 };
      }
      return this.snapshot(row);
    }
    if (message.type === 'update' && fields(message, ['version', 'type', 'request'])) { this.applySnapshot(peerId, message.request); return { received: true }; }
    if (message.type === 'catalog' && fields(message, ['version', 'type'])) return { sources: Object.values(this.data.sources).filter(s => s.active && s.peer_id === peerId).map(s => this.sourceMeta(s)) };
    fail('invalid_request', 'Unsupported peer operation.');
  }
  applySnapshot(peerId, value) {
    const row = value && this.data.requests[value.id];
    if (!row || row.sender_agent_id !== this.id || row.recipient_agent_id !== peerId || !same(row, value) || !STATES.includes(value.status) || !Number.isSafeInteger(value.revision) || value.revision < 1 || !Number.isSafeInteger(value.updated_at) || value.updated_at < row.created_at || value.updated_at > Date.now() + 300000 || !fields(value, [...canonicalKeys, 'status', 'revision', 'updated_at', ...(value.status === 'completed' ? ['result', 'attribution'] : [])])) fail('invalid_peer_response', 'The peer response did not match the original request.');
    if (value.status === 'completed') {
      if (!fields(value.result, ['text', 'source_ids', 'attachments']) || !text(value.result.text, 16384, true) || !ids(value.result.source_ids) || value.result.source_ids.some(id => !row.source_ids.includes(id)) || !Array.isArray(value.result.attachments) || value.result.attachments.length > 30 || !fields(value.attribution, ['kind', 'endpoint_id']) || value.attribution.endpoint_id !== peerId || value.attribution.kind !== (row.kind === 'ask_human' ? 'human' : 'agent')) fail('invalid_peer_response', 'Invalid result or attribution.');
      for (const item of value.result.attachments) if (!fields(item, ['id', 'filename', 'size', 'sha256']) || !value.result.source_ids.includes(item.id) || !text(item.filename, 255) || !Number.isSafeInteger(item.size) || item.size < 0 || item.size > 5 * 1024 * 1024 || !KEY.test(item.sha256)) fail('invalid_peer_response', 'Invalid attachment metadata.');
      if (row.kind === 'file' ? !value.result.source_ids.length || value.result.attachments.length !== value.result.source_ids.length || new Set(value.result.attachments.map(a => a.id)).size !== value.result.source_ids.length : value.result.source_ids.length || value.result.attachments.length) fail('invalid_peer_response', 'Result file scope does not match the request kind.');
    }
    if (row.delivered && value.revision <= row.revision) return;
    if (row.delivered && TERMINAL.includes(row.status) && (row.status !== value.status || JSON.stringify(row.result) !== JSON.stringify(value.result) || JSON.stringify(row.attribution) !== JSON.stringify(value.attribution))) fail('invalid_peer_response', 'A terminal result cannot change.');
    Object.assign(row, { status: value.status, revision: value.revision, updated_at: value.updated_at, delivered: true });
    if (value.result) { row.result = copy(value.result); row.attribution = copy(value.attribution); }
    if (TERMINAL.includes(row.status)) delete row.cancel_requested;
    this.store.save();
  }
  sync() {
    if (this.closed || !this.transport) return Promise.resolve();
    if (this.syncing) return this.syncPromise;
    this.syncing = true;
    this.syncPromise = this.syncOnce().finally(() => { this.syncing = false; });
    return this.syncPromise;
  }
  async syncOnce() {
    const deadline = Date.now() + this.options.timeout;
    const failedPeers = new Set();
    try {
      const rows = Object.values(this.data.requests); const start = (this.syncCursor || 0) % Math.max(rows.length, 1);
      for (let index = 0; index < rows.length; index++) {
        if (Date.now() >= deadline) break;
        const position = (start + index) % rows.length, row = rows[position]; this.syncCursor = position + 1;
        if (this.closed) break;
        this.expire(row);
        const incoming = row.recipient_agent_id === this.id;
        const peerId = incoming ? row.sender_agent_id : row.recipient_agent_id;
        const contact = this.data.contacts[peerId]; if (!contact?.active || failedPeers.has(peerId)) continue;
        if (incoming && !row.dirty || !incoming && TERMINAL.includes(row.status) && !row.cancel_requested) continue;
        try {
          if (incoming) {
            const revision = row.revision;
            await this.transport.rpc(contact.card, { version: 1, type: 'update', request: this.snapshot(row) });
            if (row.revision === revision) { row.dirty = false; this.store.save(); }
          } else {
            const type = row.cancel_requested && row.delivered ? 'cancel' : row.delivered ? 'get' : 'submit';
            const message = type === 'submit' ? { request: canonical(row) } : { request_id: row.id };
            this.applySnapshot(peerId, await this.transport.rpc(contact.card, { version: 1, type, ...message }));
          }
        } catch (error) { failedPeers.add(peerId); this.lastConnectionError = error instanceof SafeError ? error.code : 'peer_unavailable'; }
      }
    } finally { /* The wrapper owns the shared in-flight promise. */ }
  }
  async review(id, signal) {
    const row = this.incoming(id);
    if (!['pending_approval', 'waiting_human'].includes(row.status)) fail('review_unavailable', 'This request is not waiting for owner review.');
    if (this.reviews.has(id)) fail('review_busy', 'A review is already open.');
    this.reviews.add(id); const revision = row.revision;
    try {
      const sender = this.contact(row.sender_agent_id).card;
      const answer = await this.human({ kind: row.kind === 'ask_human' ? 'answer' : 'request', title: row.kind === 'ask_human' ? 'Your answer for ' + sender.name : 'Request from ' + sender.name,
        detail: 'Remote content — supplied by your contact:\n\n' + row.message + '\n\nKind: ' + row.kind + '\nSources: ' + (this.sourcesFor(row).map(s => s.label).join(', ') || 'none') + '\n\n' + (row.kind === 'ask_human' ? 'Only the answer you enter will be sent as your human reply.' : 'Approval allows this one request. It does not grant access to other apps, chats, or files.'), accept: row.kind === 'ask_human' ? 'Send my answer' : 'Approve once' }, signal);
      this.incoming(id); if (row.revision !== revision) fail('review_changed', 'The request changed or expired while you were reviewing it.');
      if (answer.action === 'cancel') return this.public(row);
      if (answer.action === 'deny') { row.status = 'denied'; this.changed(row); }
      else if (row.kind === 'ask_human' && answer.action === 'answer' && text(answer.text)) this.finish(row, answer.text, [], 'human');
      else if (row.kind !== 'ask_human' && answer.action === 'approve') { this.sourcesFor(row); row.status = 'ready'; row.basis = 'explicit'; this.changed(row); }
      else fail('invalid_human_response', 'No valid owner decision was received.');
      await this.sync(); return this.public(row);
    } finally { this.reviews.delete(id); }
  }
  claim(id) { const row = this.incoming(id); this.permitted(row); if (row.status === 'ready') { row.status = 'working'; this.changed(row); } return this.public(row); }
  readSources(id) { const row = this.incoming(id); this.permitted(row); return { sources: this.sourcesFor(row).map(s => ({ ...this.sourceMeta(s), ...(s.kind === 'context' ? { content: s.content } : {}) })) }; }
  finish(row, value, sourceIds, kind) {
    row.result = { text: value, source_ids: sourceIds, attachments: sourceIds.map(id => { const { id: key, filename, size, sha256 } = this.data.sources[id]; return { id: key, filename, size, sha256 }; }) };
    row.attribution = { kind, endpoint_id: this.id }; row.status = 'completed'; this.changed(row);
  }
  async complete(id, value, sourceIds = []) {
    const row = this.incoming(id); this.permitted(row);
    if (row.kind === 'ask_human' || !text(value, 16384, true) || !ids(sourceIds) || sourceIds.some(id => !row.source_ids.includes(id)) || (row.kind === 'file' ? !sourceIds.length : sourceIds.length)) fail('invalid_completion', 'Invalid result or file scope. Agents cannot provide human answers.');
    this.finish(row, value, sourceIds, 'agent'); await this.sync(); return this.public(row);
  }
  async cancel(id) {
    const row = this.requestRow(id); if (row.sender_agent_id !== this.id) fail('wrong_sender', 'Only the sender can cancel this request.');
    if (!TERMINAL.includes(row.status)) { row.cancel_requested = true; this.store.save(); await this.sync(); }
    return this.public(row);
  }
  sourceMeta(source) { const { id, label, kind, filename, size, sha256 } = source; return { id, label, kind, ...(kind === 'file' ? { filename, size, sha256 } : {}) }; }
  async publish(peerId, label, content, signal) {
    const contact = this.contact(peerId); if (!text(label, 160) || /[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/u.test(label) || content !== undefined && !text(content, 65536)) fail('invalid_arguments', 'Choose a short label and bounded context text.');
    const file = content === undefined;
    const answer = await this.human({ kind: file ? 'file' : 'confirm', title: 'Share ' + (file ? 'a selected file' : 'this context') + ' with ' + contact.card.name + '?', detail: 'Label: ' + label + '\n\n' + (file ? 'Choose the exact file to publish. Maximum 5 MiB. A private local copy will be made; the original stays untouched.' : content) + '\n\nOnly this contact may request this source. Your request approval rules still apply.', accept: file ? 'Choose file' : 'Publish context' }, signal);
    if (answer.action !== 'approve') return { published: false };
    this.contact(peerId);
    const source = { id: randomUUID(), label, peer_id: peerId, kind: file ? 'file' : 'context', active: true };
    if (file) {
      if (typeof answer.path !== 'string' || !path.isAbsolute(answer.path)) fail('file_selection_required', 'Select the file in the native owner prompt.');
      const fd = fs.openSync(answer.path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
      let bytes;
      try {
        const info = fs.fstatSync(fd); if (!info.isFile() || info.size > 5 * 1024 * 1024) fail('file_limit', 'Select a regular file no larger than 5 MiB.');
        const buffer = Buffer.alloc(5 * 1024 * 1024 + 1); let count = 0;
        while (count < buffer.length) { const read = fs.readSync(fd, buffer, count, buffer.length - count, null); if (!read) break; count += read; }
        if (count > 5 * 1024 * 1024) fail('file_limit', 'The selected file grew beyond the limit.'); bytes = buffer.subarray(0, count);
      } finally { fs.closeSync(fd); }
      if (Object.values(this.data.sources).reduce((sum, s) => sum + (s.size || 0), 0) + bytes.length > 10 * 1024 * 1024) fail('capacity', 'This preview allows 10 MiB of published files per profile.');
      Object.assign(source, { filename: safeFilename(path.basename(answer.path)), size: bytes.length, sha256: hash(bytes), data_base64: bytes.toString('base64') });
    } else source.content = content;
    if (Object.keys(this.data.sources).length >= 30) fail('capacity', 'This preview allows 30 published sources.');
    this.data.sources[source.id] = source; this.store.save(); return { published: true, source: this.sourceMeta(source) };
  }
  async revokeSource(id, signal) {
    const source = this.data.sources[id]; if (!source) fail('not_found', 'Source not found.');
    const answer = await this.human({ kind: 'confirm', title: 'Stop sharing ' + source.label + '?', detail: 'This blocks future source access and file downloads. Previously received copies cannot be retracted.', accept: 'Stop sharing' }, signal);
    if (answer.action !== 'approve') return { changed: false };
    source.active = false; this.store.save(); return { changed: true };
  }
  async download(id, sourceId) {
    const row = this.requestRow(id);
    if (row.sender_agent_id !== this.id || row.status !== 'completed' || row.kind !== 'file') fail('file_denied', 'Only the sender can download an approved completed file handoff.');
    const expected = row.result.attachments.find(a => a.id === sourceId); if (!expected) fail('file_denied', 'This file was not included in the result.');
    const previous = row.downloads?.[sourceId];
    const retained = Object.values(this.data.requests).flatMap(r => Object.values(r.downloads || {})).reduce((sum, file) => sum + file.bytes, 0);
    if (!previous && retained + expected.size > 20 * 1024 * 1024) fail('capacity', 'This preview allows 20 MiB of retained downloads per profile. No history was removed.');
    const contact = this.contact(row.recipient_agent_id);
    const value = await this.transport.rpc(contact.card, { version: 1, type: 'file', request_id: id, source_id: sourceId });
    this.contact(row.recipient_agent_id);
    if (!fields(value, ['id', 'filename', 'size', 'sha256', 'data_base64']) || typeof value.data_base64 !== 'string' || value.data_base64.length > 7 * 1024 * 1024 || value.id !== sourceId || value.sha256 !== expected.sha256 || value.size !== expected.size || value.filename !== expected.filename) fail('file_integrity', 'File metadata does not match the approved result.');
    const bytes = Buffer.from(value.data_base64, 'base64');
    if (bytes.length !== expected.size || bytes.length > 5 * 1024 * 1024 || hash(bytes) !== expected.sha256) fail('file_integrity', 'File bytes do not match the approved result.');
    if (previous) {
      if (!/^received-[a-f0-9-]{36}\.bin$/.test(previous.filename)) fail('file_integrity', 'Invalid saved download record.');
      const filename = path.join(this.directory, previous.filename);
      const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
      try {
        const info = fs.fstatSync(fd); this.store.check(info);
        const buffer = Buffer.alloc(expected.size + 1); let count = 0;
        while (count < buffer.length) { const n = fs.readSync(fd, buffer, count, buffer.length - count, null); if (!n) break; count += n; }
        if (count !== expected.size || hash(buffer.subarray(0, count)) !== expected.sha256) fail('file_integrity', 'The existing download changed. It was preserved and not overwritten.');
      } finally { fs.closeSync(fd); }
      return { path: filename, original_filename: expected.filename, bytes: bytes.length, sha256: expected.sha256, reused: true };
    }
    const basename = 'received-' + randomUUID() + '.bin', filename = path.join(this.directory, basename);
    const fd = fs.openSync(filename, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
    try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    row.downloads ||= {}; row.downloads[sourceId] = { filename: basename, bytes: bytes.length }; this.store.save();
    return { path: filename, original_filename: expected.filename, bytes: bytes.length, sha256: expected.sha256 };
  }
  async close() {
    this.closed = true; clearInterval(this.timer);
    if (this.transport) await this.transport.close();
    await this.syncPromise;
    await this.store?.close();
  }
}
