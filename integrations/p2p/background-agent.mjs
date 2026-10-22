import { SafeError } from '../desktop/server.mjs';
import { AgentRuntime } from './agent-runtime.mjs';

const TERMINAL = ['completed', 'denied', 'cancelled', 'expired'];
const LIMIT = 10;
const fail = (code, message) => { throw new SafeError(code, message); };

export class BackgroundAgent {
  constructor(engine, { runtime = new AgentRuntime(), now = Date.now } = {}) {
    this.engine = engine; this.runtime = runtime; this.now = now; this.stopped = false;
    const saved = engine.data.background_agent;
    if (saved && (typeof saved.enabled !== 'boolean' || !['codex', 'claude'].includes(saved.runtime?.provider) || typeof saved.runtime?.binary !== 'string' || !Array.isArray(saved.starts) || saved.starts.length > LIMIT || saved.starts.some(time => !Number.isSafeInteger(time) || time < 0))) fail('private_storage_error', 'Invalid background configuration. Existing data was preserved.');
    // A process crash is ambiguous: never silently replay model work or a
    // dismissed human prompt. The owner can explicitly retry from the chat.
    let changed = false;
    for (const row of Object.values(engine.data.requests)) {
      if (row.background && ['running', 'reviewing'].includes(row.background.state)) {
        row.background.state = 'interrupted'; row.background.error = 'host_stopped'; changed = true;
      }
    }
    if (changed) engine.store.save();
    this.unsubscribe = engine.subscribe(() => { this.checkActive(); this.kick(); });
    this.kick();
  }
  get config() { return this.engine.data.background_agent; }
  status() {
    const config = this.config;
    return { enabled: config?.enabled === true, provider: config?.runtime?.provider ?? null,
      runtime_version: config?.runtime?.version ?? null, running: this.active?.id ?? null,
      max_requests_per_hour: LIMIT, requests_started_last_hour: this.starts().length,
      ordinary_chat_wakeup: false, receives_while: 'desktop_extension_process_is_running',
      jobs: Object.values(this.engine.data.requests).filter(row => row.background).sort((a, b) => b.background.started_at - a.background.started_at).slice(0, 30).map(row => ({ request_id: row.id, ...row.background })),
    };
  }
  starts() { return (this.config?.starts || []).filter(time => Number.isFinite(time) && time > this.now() - 3600000); }
  async enable(provider, signal) {
    if (this.configuring) fail('review_busy', 'A background configuration review is already open.');
    this.configuring = true;
    try {
      const runtime = await this.runtime.inspect(provider, signal);
      const answer = await this.engine.human({ kind: 'confirm', title: 'Enable Airshower’s background helper?', accept: 'Enable helper',
        detail: `Use ${provider === 'codex' ? 'Codex' : 'Claude Code'} (${runtime.version}) to answer permitted incoming requests while your desktop extension is running, even when its chat is idle.\n\nRuntime: ${runtime.binary}\n\nThis uses your existing subscription allowance. At most ${LIMIT} requests start per hour. Your contact permissions still apply; requests needing approval or your personal answer open a native window.\n\nThe helper gets only the request and its exact deliberately shared context. It does not inherit this chat, read your other chats, or act in calendars, banking, or other apps. Tasks needing those actions stay for you or your main agent.\n\nIts replies may be sent automatically to the requesting contact after your existing approval rules are satisfied. Closing the desktop extension stops the helper. You can disable it from your chat.`,
      }, signal);
      if (answer.action !== 'approve') return { changed: false, ...this.status() };
      if (this.stopped) fail('cancelled', 'The host stopped during setup.');
      this.active?.controller.abort();
      if (this.running) await this.running;
      this.engine.data.background_agent = { enabled: true, runtime, starts: this.starts() };
      this.engine.store.save(); this.kick(); return { changed: true, ...this.status() };
    } finally { this.configuring = false; this.kick(); }
  }
  async disable() {
    if (this.configuring) fail('review_busy', 'Finish the background setup prompt before changing it.');
    if (this.config) { this.config.enabled = false; this.engine.store.save(); }
    this.active?.controller.abort(); clearTimeout(this.rateTimer);
    if (this.running) await this.running;
    return this.status();
  }
  async retry(id, signal) {
    if (!this.config?.enabled) fail('background_disabled', 'Enable the background helper first.');
    const row = this.engine.incoming(id);
    if (!row.background || ['running', 'reviewing', 'done'].includes(row.background.state) || TERMINAL.includes(row.status)) fail('retry_unavailable', 'This request is not waiting for a background retry.');
    const current = row.background;
    const answer = await this.engine.human({ kind: 'confirm', title: 'Retry this background request?', accept: 'Retry once',
      detail: 'Remote request:\n\n' + row.message + '\n\nThe previous run stopped or needs attention. A retry can use more of your subscription allowance. Existing contact and source permissions will be checked again.' }, signal);
    if (answer.action !== 'approve') return { changed: false };
    this.engine.incoming(id);
    if (this.stopped || !this.config?.enabled || row.background !== current || TERMINAL.includes(row.status)) fail('review_changed', 'The request or background setting changed during review.');
    delete row.background;
    if (row.status === 'working') { row.status = 'ready'; this.engine.changed(row); }
    else this.engine.store.save();
    this.kick(); return { changed: true };
  }
  checkActive() {
    if (!this.active) return;
    const { id, controller, reviewing } = this.active;
    try {
      if (this.stopped || this.engine.closed || !this.config?.enabled) throw new Error();
      const row = this.engine.incoming(id);
      if (TERMINAL.includes(row.status)) throw new Error();
      this.engine.sourcesFor(row);
      if (!reviewing) this.engine.permitted(row);
    } catch { controller.abort(); }
  }
  kick() {
    if (this.stopped || this.configuring || !this.config?.enabled || this.engine.closed) return;
    // A notification can arrive between drain's last queue scan and its promise
    // settling. Remember it so that work cannot become stranded in that gap.
    if (this.running) { this.wakePending = true; return; }
    if (this.scheduled) return;
    this.scheduled = setImmediate(() => {
      this.scheduled = null;
      if (this.stopped || this.engine.closed || !this.config?.enabled || this.configuring) return;
      this.wakePending = false;
      this.running = this.drain().catch(() => { /* Persistent storage failures stop work without exposing private diagnostics. */ }).finally(() => {
        this.running = null;
        if (this.wakePending) { this.wakePending = false; this.kick(); }
      });
    });
  }
  async drain() {
    while (!this.stopped && !this.engine.closed && this.config?.enabled && !this.configuring) {
      const row = Object.values(this.engine.data.requests).find(row => row.recipient_agent_id === this.engine.id && this.engine.data.contacts[row.sender_agent_id]?.active && !row.background && ['pending_approval', 'waiting_human', 'ready'].includes(row.status));
      if (!row) return;
      this.engine.expire(row); if (TERMINAL.includes(row.status)) continue;
      const starts = this.starts();
      if (starts.length >= LIMIT) {
        clearTimeout(this.rateTimer);
        this.rateTimer = setTimeout(() => this.kick(), Math.max(1000, starts[0] + 3600001 - this.now())); this.rateTimer.unref?.(); return;
      }
      this.config.starts = [...starts, this.now()];
      await this.process(row);
    }
  }
  async process(row) {
    const e = this.engine, controller = new AbortController();
    const reviewing = ['pending_approval', 'waiting_human'].includes(row.status);
    this.active = { id: row.id, controller, reviewing };
    row.background = { state: reviewing ? 'reviewing' : 'running', started_at: this.now() }; e.store.save();
    const allowed = () => {
      this.checkActive(); if (controller.signal.aborted) fail('cancelled', 'The background request was cancelled or its permissions changed.');
    };
    try {
      allowed();
      if (reviewing) {
        await e.review(row.id, controller.signal);
        if (TERMINAL.includes(row.status)) { row.background.state = 'done'; return; }
        allowed();
        if (row.status !== 'ready') { row.background.state = 'dismissed'; return; }
      }
      this.active.reviewing = false; allowed();
      // The engine owns the actual result and attribution. The model never sees
      // keys, executable paths, arbitrary source paths, or completion tools.
      e.claim(row.id); row.background.state = 'running'; e.store.save();
      e.backgroundRequest = row.id;
      let answer;
      if (row.kind === 'file') answer = { outcome: 'reply', text: 'The approved file is ready to receive.' };
      else {
        const sources = e.readSources(row.id).sources;
        if (Buffer.byteLength(JSON.stringify(sources)) > 256 * 1024) fail('background_context_limit', 'This request exceeds the background context limit. Use the main chat.');
        answer = await this.runtime.run(this.config.runtime, { kind: row.kind, message: row.message, sources }, { directory: e.directory, signal: controller.signal });
      }
      allowed();
      if (!answer || !['reply', 'needs_owner'].includes(answer.outcome) || typeof answer.text !== 'string' || !answer.text.trim() || Buffer.byteLength(JSON.stringify(answer.text)) > 16386) fail('runtime_invalid_answer', 'No bounded answer was produced.');
      if (answer.outcome === 'needs_owner' || row.kind === 'task') {
        row.background.state = 'needs_owner'; row.background.draft = answer.text;
        e.store.save();
        // This is an attention notice, not an approval or an external send.
        // Its dismissal has no effect on the request or on the draft.
        await e.human({ kind: 'confirm', title: 'Airshower needs your main agent', accept: 'Keep for my chat',
          detail: 'No answer has been sent and no external action has been performed. Ask your main agent to check Airshower background status.\n\nRemote request:\n' + row.message + '\n\nBackground agent’s draft (not a human answer):\n' + answer.text }, controller.signal);
      } else {
        await e.complete(row.id, answer.text, row.kind === 'file' ? row.source_ids : [], true);
        row.background.state = 'done';
      }
    } catch (error) {
      row.background.state = TERMINAL.includes(row.status) ? 'done' : controller.signal.aborted ? 'interrupted' : 'error';
      row.background.error = error instanceof SafeError ? error.code : 'background_failed';
      e.store.save();
      if (row.background.state === 'error') {
        const detail = error instanceof SafeError ? error.message : 'The background helper could not finish. No answer was sent.';
        try { await e.human({ kind: 'confirm', title: 'Airshower’s helper needs attention', accept: 'Keep for my chat',
          detail: detail + '\n\nAsk your main agent to check Airshower background status. After resolving the problem, it can offer a retry. The request and its history were preserved.' }, controller.signal); } catch {}
      }
    } finally {
      e.backgroundRequest = null; this.active = null;
      row.background.finished_at = this.now();
      e.store.save();
    }
  }
  async close() {
    this.stopped = true; this.unsubscribe(); clearImmediate(this.scheduled); clearTimeout(this.rateTimer);
    this.active?.controller.abort(); if (this.running) await this.running;
  }
}
