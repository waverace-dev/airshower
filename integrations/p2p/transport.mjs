import { SafeError, strictJSON } from '../desktop/server.mjs';

const ALPN = [...Buffer.from('airshower/p2p/1')];
const LIMIT = 8 * 1024 * 1024;
let loggingInitialized = false;
export async function bounded(promise, ms, cleanup = () => {}) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => { cleanup(); reject(new SafeError('peer_unavailable', 'The peer did not respond before the deadline. Work remains queued locally.')); }, ms); })]); }
  finally { clearTimeout(timer); }
}
export class PeerTransport {
  constructor({ secret, authorize, receive, mode = 'internet', bindAddr, timeout = 8000 }) {
    this.secret = secret; this.authorize = authorize; this.receive = receive; this.mode = mode;
    this.bindAddr = bindAddr; this.timeout = timeout; this.connections = new Map(); this.allConnections = new Map(); this.dials = new Map(); this.nativeDials = new Set(); this.paths = new Map(); this.tasks = new Set(); this.activeStreams = 0; this.closed = false;
  }
  async start() {
    // Upstream 1.1.0 publishes index.js at the package root despite its main
    // field pointing at iroh-js/index.js. Use the verified, pinned file directly.
    this.iroh = (await import('./node_modules/@number0/iroh/index.js')).default;
    // The native library installs a process-global subscriber and aborts if
    // setLogLevel is called twice (including when opening a second endpoint).
    if (!loggingInitialized) { this.iroh.setLogLevel('Off'); loggingInitialized = true; }
    const builder = this.iroh.Endpoint.builder();
    if (this.mode === 'local') builder.applyMinimal(); else builder.applyN0();
    builder.secretKey([...Buffer.from(this.secret, 'hex')]); builder.alpns([ALPN]);
    if (this.bindAddr || this.mode === 'local') builder.bindAddr(this.bindAddr || '127.0.0.1:0');
    this.endpoint = await builder.bind(); this.id = this.endpoint.id().toString();
    this.listening = this.acceptLoop();
    return this;
  }
  card(name) {
    return { protocol: 'airshower/p2p/1', name, endpoint_id: this.id,
      ticket: this.iroh.EndpointTicket.fromAddr(this.endpoint.addr()).toString() };
  }
  parseCard(value) {
    if (typeof value === 'string') { if (Buffer.byteLength(value) > 8192) throw new Error('Invalid card'); value = strictJSON(value); }
    if (!value || Object.keys(value).sort().join(',') !== 'endpoint_id,name,protocol,ticket' || value.protocol !== 'airshower/p2p/1' || typeof value.name !== 'string' || !value.name.trim() || Buffer.byteLength(value.name) > 160 || /[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]/u.test(value.name) || !/^[a-f0-9]{64}$/.test(value.endpoint_id) || typeof value.ticket !== 'string' || value.ticket.length > 8000) throw new Error('Invalid card');
    const address = this.iroh.EndpointTicket.fromString(value.ticket).endpointAddr();
    if (address.id().toString() !== value.endpoint_id || value.endpoint_id === this.id) throw new Error('Invalid card identity');
    // A ticket can contain a relay address. Do not let a contact redirect us to
    // arbitrary HTTP/private services; only Iroh's documented public relays.
    if (address.relayUrl()) {
      const url = new URL(address.relayUrl());
      const host = url.hostname.replace(/\.$/, '');
      const relays = ['use1-1', 'usw1-1', 'euc1-1', 'aps1-1'].map(region => region + '.relay.n0.iroh.link');
      if (url.protocol !== 'https:' || !relays.includes(host) || url.username || url.password || url.port || url.search || url.hash || url.pathname !== '/') throw new Error('Unsupported relay in contact card');
    }
    return { ...value };
  }
  remember(connection) {
    const id = connection.remoteId().toString();
    if (this.closed || !this.authorize(id)) { connection.close(1n, []); throw new SafeError('not_connected', 'Both owners must connect their contact cards.'); }
    if (this.allConnections.size >= 32 || [...this.allConnections.values()].filter(peer => peer === id).length >= 2) { connection.close(1n, []); throw new Error('Connection limit'); }
    this.connections.set(id, connection);
    this.allConnections.set(connection, id);
    connection.setMaxConcurrentBiStreams(4n); connection.setMaxConcurrentUniStreams(0n); connection.setReceiveWindow(8n * 1024n * 1024n);
    const task = this.streamLoop(connection, id).finally(() => { this.tasks.delete(task); this.allConnections.delete(connection); if (this.connections.get(id) === connection) this.connections.delete(id); });
    this.tasks.add(task);
    return connection;
  }
  async acceptLoop() {
    let handshakes = 0;
    while (!this.closed) {
      let incoming;
      try { incoming = await this.endpoint.acceptNext(); } catch { break; }
      if (!incoming) break;
      if (handshakes >= 16) { await incoming.refuse().catch(() => {}); continue; }
      handshakes++;
      let expired = false;
      const timer = setTimeout(() => { expired = true; }, this.timeout);
      // Keep the slot until native completion, even after our deadline. The
      // binding cannot abort a pending handshake independently of the endpoint.
      const task = (async () => {
        const connection = await (await incoming.accept()).connect();
        if (expired || this.closed) { connection.close(2n, []); return; }
        this.remember(connection);
      })().catch(() => {}).finally(() => { clearTimeout(timer); handshakes--; this.tasks.delete(task); });
      this.tasks.add(task);
    }
  }
  async streamLoop(connection, id) {
    const active = new Set();
    try {
      while (!this.closed) {
        const stream = await connection.acceptBi();
        if (!this.authorize(id) || active.size >= 4 || this.activeStreams >= 8) { await stream.recv.stop(1n).catch(() => {}); await stream.send.reset(1n).catch(() => {}); continue; }
        this.activeStreams++;
        let expired = false;
        const task = bounded((async () => {
          let result;
          try {
            const raw = await stream.recv.readToEnd(LIMIT);
            if (expired || this.closed) return;
            if (!this.authorize(id)) throw new SafeError('not_connected', 'Contact revoked');
            result = { ok: true, value: await this.receive(id, strictJSON(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(raw)))) };
          } catch (error) { result = { ok: false, code: error instanceof SafeError ? error.code : 'invalid_request' }; }
          const bytes = Buffer.from(JSON.stringify(result)); if (bytes.length > LIMIT) throw new Error('Response too large');
          await stream.send.writeAll([...bytes]); await stream.send.finish();
        })(), this.timeout, () => { expired = true; stream.recv.stop(1n).catch(() => {}); stream.send.reset(1n).catch(() => {}); }).catch(() => {}).finally(() => { this.activeStreams--; active.delete(task); });
        active.add(task);
      }
    } catch { /* Connection close is the normal end of this loop. */ }
    await Promise.allSettled(active);
  }
  async rpc(card, message) {
    if (!this.authorize(card.endpoint_id)) throw new SafeError('not_connected', 'This contact is not connected.');
    let connection = this.connections.get(card.endpoint_id), expired = false;
    const work = (async () => {
      if (!connection || connection.closeReason() != null) {
        if (!this.dials.has(card.endpoint_id)) {
          if (this.nativeDials.size >= 16) throw new SafeError('peer_unavailable', 'Connection attempts are at capacity. Work remains queued locally.');
          let late = false;
          const native = (async () => {
            const address = this.iroh.EndpointTicket.fromString(card.ticket).endpointAddr();
            const connected = await this.endpoint.connect(address, ALPN);
            if (late || this.closed || connected.remoteId().toString() !== card.endpoint_id) { connected.close(2n, []); throw new Error('Connection expired or identity mismatch'); }
            return this.remember(connected);
          })();
          this.nativeDials.add(native); native.finally(() => this.nativeDials.delete(native)).catch(() => {});
          const dial = bounded(native, this.timeout, () => { late = true; }).finally(() => this.dials.delete(card.endpoint_id));
          this.dials.set(card.endpoint_id, dial);
        }
        connection = await this.dials.get(card.endpoint_id);
      }
      if (expired || this.closed || !this.authorize(card.endpoint_id)) throw new Error('Operation expired');
      const stream = await connection.openBi();
      if (expired || this.closed || !this.authorize(card.endpoint_id)) { await stream.send.reset(1n); throw new Error('Operation expired'); }
      const bytes = Buffer.from(JSON.stringify(message)); if (bytes.length > LIMIT) throw new Error('Request too large');
      await stream.send.writeAll([...bytes]); await stream.send.finish();
      const raw = await stream.recv.readToEnd(LIMIT);
      this.paths.set(card.endpoint_id, connection.paths().map(p => ({ selected: p.isSelected, type: p.isRelay ? 'relay' : p.isIp ? 'direct' : 'other' })));
      const result = strictJSON(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(raw)));
      if (!result || result.ok !== true) throw new SafeError('peer_rejected', 'The other bridge declined this operation. Check both contact approvals and the request state.');
      return result.value;
    })();
    return bounded(work, this.timeout, () => { expired = true; if (connection) connection.close(2n, []); });
  }
  disconnect(id) { for (const [connection, peer] of this.allConnections) if (peer === id) connection.close(1n, []); this.connections.delete(id); this.paths.delete(id); }
  async close() {
    this.closed = true;
    for (const connection of this.allConnections.keys()) connection.close(0n, []);
    if (this.endpoint) await this.endpoint.close();
    await this.listening;
    await Promise.allSettled([...this.nativeDials]);
    await Promise.allSettled([...this.tasks]);
  }
}
