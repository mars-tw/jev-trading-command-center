'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'web', 'timeframe-research.js'), 'utf8');
const KEYS = ['5m', '15m', '30m', '45m', '1h', '2h', '4h', '8h', '16h', '1d'];

function frame(interval, overrides = {}) {
  const actual = overrides.actual_asof ?? 1792465199999;
  const expected = overrides.expected_asof ?? actual;
  return {
    interval,
    status: overrides.status || 'ready',
    reason: overrides.reason ?? null,
    metadata: {
      source_interval: interval === '16h' ? '8h' : interval,
      input_count: 121,
      source_closed_count: 121,
      closed_count: 120,
      coverage: {
        first_open_at: actual - 1200000,
        last_closed_at: actual,
        selected_bars: 120,
        contiguous: true
      },
      last_closed_at: actual,
      actual_asof: actual,
      expected_asof: expected,
      stale: overrides.stale ?? false,
      unknown_reason: overrides.unknown_reason ?? null
    },
    features: overrides.features || {
      price: 92.54,
      ema20: 91.82662850077192,
      ema50: 90.7730668180282,
      macd: 0.49516587285201297,
      macd_signal: 0.4903082223723484,
      macd_hist: 0.004857650479664577,
      rsi14: 72.47706422018337,
      atr: 0.34071428571428647,
      atr_pct: 0.36818055512674136,
      trend: 'up',
      breakout: 'none',
      pattern: 'none'
    },
    analysis_only: true,
    execution_authority: false
  };
}

function connector(overrides = {}) {
  return {
    status: 'capability_unverified',
    spawned: false,
    analysis_only: true,
    execution_authority: false,
    protocol_id: 'AGY',
    display_name: 'Antigravity',
    version: '1.4.2',
    enabled: true,
    reason: 'capability_unverified',
    context_ready: true,
    frame_count: 10,
    research_request_enabled: false,
    native_invocation_available: false,
    capability: {
      connector_enabled: true,
      capability_verified: false,
      spawn_allowed: false,
      tools_allowed: false,
      mcp_allowed: false,
      plugins_allowed: false,
      model: 'UNVERIFIED'
    },
    ...overrides
  };
}

function packet({ source = 'DEMO', symbol = 'DEMO1USDT', status = 'ready', reason = null, frameOverrides = {}, antigravity = connector() } = {}) {
  const sample_frames = {};
  for (const key of KEYS) sample_frames[key] = frame(key, frameOverrides[key] || {});
  return {
    symbol,
    status,
    reason,
    source,
    server_time: 1792465356423,
    analyzed_at: 1792465356400,
    server_cutoff: 1792465199999,
    fetched_at: 1792465356434,
    stale: false,
    data_stale: false,
    cache_stale: false,
    analysis_only: true,
    execution_authority: false,
    canonical_frame_keys: KEYS.slice(),
    sample_frames,
    antigravity
  };
}

function unknownPacket(source = 'DEMO') {
  return {
    symbol: null,
    status: 'unknown',
    reason: 'missing_source',
    source,
    server_time: 1792465356423,
    fetched_at: null,
    stale: false,
    data_stale: false,
    cache_stale: false,
    analysis_only: true,
    execution_authority: false,
    antigravity: connector({ context_ready: false, frame_count: 0 })
  };
}

class FakeClassList {
  constructor(owner) { this.owner = owner; }
  values() { return new Set(String(this.owner.className || '').split(/\s+/).filter(Boolean)); }
  contains(value) { return this.values().has(value); }
  add(...values) { const next = this.values(); for (const value of values) next.add(value); this.owner.className = [...next].join(' '); }
  remove(...values) { const next = this.values(); for (const value of values) next.delete(value); this.owner.className = [...next].join(' '); }
  toggle(value, force) {
    const present = this.contains(value);
    const next = force === undefined ? !present : !!force;
    if (next) this.add(value); else this.remove(value);
    return next;
  }
}

class FakeNode {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.className = '';
    this.classList = new FakeClassList(this);
    this._text = '';
    this.id = '';
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.open = false;
  }
  set innerHTML(_) { throw new Error('Unsafe innerHTML assignment'); }
  get innerHTML() { throw new Error('Unsafe innerHTML read'); }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  get nextSibling() {
    if (!this.parentNode) return null;
    const index = this.parentNode.children.indexOf(this);
    return index >= 0 ? this.parentNode.children[index + 1] || null : null;
  }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  appendChild(node) {
    if (!(node instanceof FakeNode)) {
      const text = new FakeNode('#text', this.ownerDocument);
      text.textContent = node;
      node = text;
    }
    if (node.parentNode) {
      const oldIndex = node.parentNode.children.indexOf(node);
      if (oldIndex >= 0) node.parentNode.children.splice(oldIndex, 1);
    }
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  insertBefore(node, before) {
    if (!before) return this.appendChild(node);
    const index = this.children.indexOf(before);
    if (index < 0) return this.appendChild(node);
    node.parentNode = this;
    this.children.splice(index, 0, node);
    return node;
  }
  insertAdjacentElement(position, node) {
    if (position !== 'afterend' || !this.parentNode) throw new Error('unsupported insertion');
    this.parentNode.insertBefore(node, this.nextSibling);
    return node;
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._text = '';
    this.append(...nodes);
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'id') this.id = String(value);
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatchEvent(event) {
    if (!event || !event.type) throw new Error('event type required');
    if (!event.target) event.target = this;
    if (!event.preventDefault) event.preventDefault = () => { event.defaultPrevented = true; };
    for (const handler of this.listeners.get(event.type) || []) handler.call(this, event);
    return !event.defaultPrevented;
  }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
  walk(output = []) { output.push(this); for (const child of this.children) child.walk(output); return output; }
}

class FakeDocument extends FakeNode {
  constructor() {
    super('#document', null);
    this.ownerDocument = this;
    this.readyState = 'complete';
    this.hidden = false;
    this.activeElement = null;
    this.body = new FakeNode('body', this);
    this.main = new FakeNode('main', this);
    this.body.appendChild(this.main);
    this.appendChild(this.body);
  }
  createElement(tagName) { return new FakeNode(tagName, this); }
  createTextNode(text) { const node = new FakeNode('#text', this); node.textContent = text; return node; }
  querySelector(selector) {
    if (selector === 'main') return this.main;
    if (selector.startsWith('#')) return this.getElementById(selector.slice(1));
    return null;
  }
  getElementById(id) { return this.walk([]).find((node) => node.id === id) || null; }
}

class FakeEventTarget {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    for (const handler of this.listeners.get(event.type) || []) handler.call(this, event);
  }
}

function makeEnvironment({ demo = true, readOnly = true } = {}) {
  const document = new FakeDocument();
  const eventTarget = new FakeEventTarget();
  const timers = new Map();
  const queue = [];
  const calls = [];
  let timerId = 0;
  let now = 1792465356500;
  let locked = false;
  let wrapperCalls = 0;
  let nativeFetchCalls = 0;
  const registered = [];

  class FakeDate extends Date {
    static now() { return now; }
  }
  const media = new FakeEventTarget();
  media.matches = false;

  const window = {
    document,
    __JEV_DEMO__: demo,
    __JEV_MODE__: demo ? 'DEMO' : 'READ_ONLY_REAL',
    __companionReadback: {
      ready: true,
      crew: [{ alias: 'AGY', provenance: 'UNVERIFIED', pid: 4321, job: '<img src=x onerror=attack()>' }]
    },
    JEVCompanion: {
      registerPanel(id, label, node) { registered.push({ id, label, node }); return true; }
    },
    JEVRemote: {
      ready: Promise.resolve(true),
      isLocked: () => locked,
      isReadOnly: () => readOnly,
      async fetch(url, init) {
        wrapperCalls += 1;
        const copiedHeaders = { ...(init.headers || {}), 'X-JEV-CSRF': 'wrapper-added' };
        calls.push({ url, method: init.method, headers: copiedHeaders, body: init.body, at: now });
        const next = queue.shift();
        if (!next) throw new Error('missing fake response');
        return typeof next === 'function' ? next(url, init) : next;
      }
    },
    fetch() { nativeFetchCalls += 1; throw new Error('native fetch must not bypass remote wrapper'); },
    matchMedia: () => media,
    setTimeout(callback, delay = 0) {
      const id = ++timerId;
      timers.set(id, { callback, delay: Math.max(0, Number(delay) || 0), due: now + Math.max(0, Number(delay) || 0) });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    addEventListener: eventTarget.addEventListener.bind(eventTarget),
    dispatchEvent: eventTarget.dispatchEvent.bind(eventTarget)
  };
  window.window = window;

  const context = {
    window,
    document,
    AbortController,
    Date: FakeDate,
    Intl,
    Promise,
    Object,
    Array,
    Set,
    Map,
    String,
    Number,
    Boolean,
    Math,
    JSON,
    RegExp,
    Error
  };
  vm.runInNewContext(SOURCE, context, { filename: 'timeframe-research.js' });

  function response(payload, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => payload };
  }
  async function flush() {
    for (let index = 0; index < 12; index += 1) await Promise.resolve();
  }
  async function ready() { await flush(); }
  function enqueue(payload, status = 200) { queue.push(response(payload, status)); }
  function enqueueRaw(value) { queue.push(value); }
  async function runNextTimer() {
    const next = [...timers.entries()].sort((left, right) => left[1].due - right[1].due)[0];
    if (!next) return false;
    const [id, timer] = next;
    timers.delete(id);
    now = timer.due;
    timer.callback();
    await flush();
    return true;
  }
  function dispatchVisibility(hidden) {
    document.hidden = hidden;
    document.dispatchEvent({ type: 'visibilitychange' });
  }
  return {
    window,
    document,
    timers,
    calls,
    registered,
    media,
    ready,
    enqueue,
    enqueueRaw,
    response,
    runNextTimer,
    dispatchVisibility,
    setLocked(value) { locked = value; },
    now: () => now,
    wrapperCalls: () => wrapperCalls,
    nativeFetchCalls: () => nativeFetchCalls
  };
}

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

test('uses exactly ten canonical cards and one shared 60-minute/1-hour label', async () => {
  const env = makeEnvironment();
  await env.ready();
  const canonical = JSON.parse(JSON.stringify(env.window.JEVTimeframeResearch._test.CANONICAL));
  assert.deepEqual(canonical.map((item) => item.key), KEYS);
  assert.equal(canonical.length, 10);
  assert.equal(canonical.filter((item) => item.key === '1h').length, 1);
  assert.equal(canonical.find((item) => item.key === '1h').label, '60分鐘／1小時');
  assert.equal(canonical.find((item) => item.key === '1d').label, '24小時');
  assert.equal(canonical.some((item) => item.key === '60m'), false);
  const cards = env.document.getElementById('timeframeGrid').children;
  assert.equal(cards.length, 10);
  assert.deepEqual(cards.map((card) => card.dataset.interval), KEYS);
  assert.deepEqual(env.registered.map(({ id, label }) => ({ id, label })), [{ id: 'timeframeResearch', label: '週期' }]);
});

test('fails closed on DEMO/public source taint and accepts truthful SYNTHETIC demo packets', async () => {
  const env = makeEnvironment({ demo: true });
  await env.ready();
  const api = env.window.JEVTimeframeResearch._test;
  const tainted = api.normalizePacket(packet({ source: 'PUBLIC_BINANCE', symbol: 'BTCUSDT' }), 'DEMO');
  assert.equal(tainted.status, 'unknown');
  assert.equal(tainted.reason, 'source_mismatch');
  assert.equal(tainted.tainted, true);
  assert.equal(Object.values(tainted.frames).some((item) => item.features), false);
  const synthetic = api.normalizePacket(packet({ source: 'SYNTHETIC' }), 'DEMO');
  assert.equal(synthetic.valid, true);
  assert.equal(synthetic.source, 'SYNTHETIC');
  const realEnv = makeEnvironment({ demo: false });
  await realEnv.ready();
  const realTaint = realEnv.window.JEVTimeframeResearch._test.normalizePacket(packet(), 'PUBLIC_BINANCE');
  assert.equal(realTaint.reason, 'source_mismatch');
});

test('derives freshness from actual/expected as-of and withholds invalid or missing-latest indicators', async () => {
  const env = makeEnvironment();
  await env.ready();
  const api = env.window.JEVTimeframeResearch._test;
  const tenHoursAgo = env.now() - 10 * 60 * 60 * 1000;
  assert.equal(api.staleFromAsOf(tenHoursAgo, tenHoursAgo, false), false);
  assert.equal(api.staleFromAsOf(tenHoursAgo, tenHoursAgo + 1, false), true);
  const oldButFresh = api.normalizeFrame(frame('16h', { actual_asof: tenHoursAgo, expected_asof: tenHoursAgo }), '16h', 'DEMO');
  assert.equal(oldButFresh.status, 'ready');
  assert.equal(oldButFresh.dataStale, false);
  const missing = api.normalizeFrame(frame('5m', { unknown_reason: 'missing_latest' }), '5m', 'DEMO');
  assert.equal(missing.status, 'unknown');
  assert.equal(missing.features, null);
  const hostileFeatures = { ...frame('5m').features, ema20: Infinity, pattern: '<img onerror=attack()>' };
  const invalid = api.normalizeFrame(frame('5m', { features: hostileFeatures }), '5m', 'DEMO');
  assert.equal(invalid.status, 'unknown');
  assert.equal(invalid.features, null);
});

test('ready frames require equal integral timestamps and ignore stale or historical age', async () => {
  const env = makeEnvironment();
  await env.ready();
  const api = env.window.JEVTimeframeResearch._test;
  const fields = ['actual_asof', 'expected_asof', 'last_closed_at'];
  const invalidValues = [
    ['missing'],
    ['null', null],
    ['string', '1792465199999'],
    ['NaN', NaN],
    ['Infinity', Infinity],
    ['fraction', 1792465199999.5],
    ['negative', -1],
    ['out-of-range', 8640000000000001]
  ];
  for (const field of fields) {
    for (const [label, value] of invalidValues) {
      const raw = frame('5m');
      if (label === 'missing') delete raw.metadata[field];
      else raw.metadata[field] = value;
      const normalized = api.normalizeFrame(raw, '5m', 'DEMO');
      assert.equal(normalized.status, 'unknown', `${field} ${label} must not stay ready`);
      assert.equal(normalized.reason, 'invalid', `${field} ${label} reason`);
      assert.equal(normalized.features, null, `${field} ${label} features`);
    }
  }

  const actualAhead = frame('5m');
  actualAhead.metadata.actual_asof = 1792465200000;
  actualAhead.metadata.last_closed_at = 1792465200000;
  const actualAheadNorm = api.normalizeFrame(actualAhead, '5m', 'DEMO');
  assert.equal(actualAheadNorm.status, 'unknown');
  assert.equal(actualAheadNorm.reason, 'missing_latest_closed');
  assert.equal(actualAheadNorm.features, null);

  const expectedAhead = frame('5m');
  expectedAhead.metadata.expected_asof = 1792465200000;
  const expectedAheadNorm = api.normalizeFrame(expectedAhead, '5m', 'DEMO');
  assert.equal(expectedAheadNorm.status, 'unknown');
  assert.equal(expectedAheadNorm.reason, 'missing_latest_closed');
  assert.equal(expectedAheadNorm.features, null);

  const lastClosedBehind = frame('5m');
  lastClosedBehind.metadata.last_closed_at = 1792465199998;
  const lastClosedBehindNorm = api.normalizeFrame(lastClosedBehind, '5m', 'DEMO');
  assert.equal(lastClosedBehindNorm.status, 'unknown');
  assert.equal(lastClosedBehindNorm.reason, 'invalid');
  assert.equal(lastClosedBehindNorm.features, null);

  const lastClosedAhead = frame('5m');
  lastClosedAhead.metadata.last_closed_at = 1792465200000;
  const lastClosedAheadNorm = api.normalizeFrame(lastClosedAhead, '5m', 'DEMO');
  assert.equal(lastClosedAheadNorm.status, 'unknown');
  assert.equal(lastClosedAheadNorm.reason, 'invalid');
  assert.equal(lastClosedAheadNorm.features, null);

  const oldTs = 1000000000000;
  const oldEqual = frame('16h');
  oldEqual.metadata.actual_asof = oldTs;
  oldEqual.metadata.expected_asof = oldTs;
  oldEqual.metadata.last_closed_at = oldTs;
  const oldNorm = api.normalizeFrame(oldEqual, '16h', 'DEMO');
  assert.equal(oldNorm.status, 'ready');
  assert.equal(oldNorm.dataStale, false);
  assert.equal(oldNorm.features === null, false);

  const staleMeta = frame('5m');
  staleMeta.metadata.stale = true;
  const staleNorm = api.normalizeFrame(staleMeta, '5m', 'DEMO');
  assert.equal(staleNorm.status, 'ready');
  assert.equal(staleNorm.dataStale, true);
  assert.equal(staleNorm.features === null, false);

  const stalePacket = packet();
  stalePacket.stale = true;
  stalePacket.cache_stale = true;
  const stalePacketNorm = api.normalizePacket(stalePacket, 'DEMO');
  assert.equal(stalePacketNorm.stale, true);
  assert.equal(stalePacketNorm.cacheStale, true);
  for (const key of KEYS) {
    assert.equal(stalePacketNorm.frames[key].status, 'ready');
    assert.equal(stalePacketNorm.frames[key].features === null, false);
  }

  const keptUnknown = frame('5m', { status: 'unknown', unknown_reason: 'cache_miss' });
  keptUnknown.metadata.expected_asof = keptUnknown.metadata.actual_asof + 1;
  const keptUnknownNorm = api.normalizeFrame(keptUnknown, '5m', 'DEMO');
  assert.equal(keptUnknownNorm.status, 'unknown');
  assert.equal(keptUnknownNorm.reason, 'cache_miss');
  assert.equal(keptUnknownNorm.features, null);
});

test('formats only finite numbers and safe UTC timestamps', async () => {
  const env = makeEnvironment();
  await env.ready();
  const api = env.window.JEVTimeframeResearch._test;
  assert.equal(api.formatNumber(NaN), '—');
  assert.equal(api.formatNumber(Infinity), '—');
  assert.equal(api.formatNumber(-Infinity), '—');
  assert.match(api.formatNumber(0.0000123456), /^0[.,]00001235$/);
  assert.equal(api.formatUtc(Infinity), '—');
  assert.equal(api.formatUtc(-1), '—');
  assert.match(api.formatUtc(1792465199999), / UTC$/);
});

test('treats Antigravity fields as configuration, never native/model proof', async () => {
  const env = makeEnvironment();
  await env.ready();
  const api = env.window.JEVTimeframeResearch._test;
  const normalized = api.normalizeConnector(connector({
    spawned: true,
    research_request_enabled: true,
    native_invocation_available: true,
    capability: { capability_verified: true, tools_allowed: true, mcp_allowed: true, plugins_allowed: true, model: 'claimed-model' }
  }));
  assert.equal(normalized.enabled, true);
  assert.equal(normalized.compatibilityVersion, '1.4.2');
  assert.equal(normalized.model, 'UNVERIFIED');
  assert.equal(normalized.spawned, false);
  assert.equal(normalized.researchRequestEnabled, false);
  assert.equal(normalized.nativeInvocationAvailable, false);
  assert.equal(normalized.proofContradiction, true);
});

test('uses safe DOM text, relative endpoints, remote CSRF wrapper, and permits read-only research selection', async () => {
  const env = makeEnvironment({ readOnly: true });
  await env.ready();
  assert.equal(env.calls.length, 0, 'closed panel must not fetch');
  const panel = env.document.getElementById('timeframeResearch');
  env.enqueue(unknownPacket());
  panel.open = true;
  panel.dispatchEvent({ type: 'toggle' });
  await env.ready();
  assert.equal(env.calls.length, 1);
  assert.deepEqual({ url: env.calls[0].url, method: env.calls[0].method }, { url: '/api/analysis/timeframes', method: 'GET' });
  assert.equal(env.calls[0].url.includes('?'), false);
  assert.equal(env.calls.some((call) => call.method === 'POST'), false);

  env.enqueue(packet());
  const input = env.document.getElementById('timeframeSymbol');
  input.value = 'demo1usdt';
  env.document.getElementById('timeframeView').dispatchEvent({ type: 'click' });
  await env.ready();
  assert.equal(env.calls.length, 2);
  assert.deepEqual({ url: env.calls[1].url, method: env.calls[1].method }, { url: '/api/analysis/select', method: 'POST' });
  assert.equal(env.calls[1].url.startsWith('/'), true);
  assert.equal(env.calls[1].url.includes('://'), false);
  assert.deepEqual(JSON.parse(env.calls[1].body), { symbol: 'DEMO1USDT' });
  assert.deepEqual(Object.keys(JSON.parse(env.calls[1].body)), ['symbol']);
  assert.equal(env.calls[1].headers['X-JEV-CSRF'], 'wrapper-added');
  assert.equal(env.wrapperCalls(), 2);
  assert.equal(env.nativeFetchCalls(), 0);
  assert.equal(env.window.JEVTimeframeResearch._controller.selectedSymbol(), 'DEMO1USDT');
  assert.equal(env.document.getElementById('timeframeGrid').children.length, 10);
  assert.equal(env.document.body.walk([]).some((node) => node.tagName === 'IMG'), false);
  assert.match(env.document.getElementById('timeframeConnector').textContent, /模型 UNVERIFIED/);
});

test('keeps missing_latest_closed frames unknown and renders no numeric indicators', async () => {
  const env = makeEnvironment();
  await env.ready();
  const missing = packet();
  const sentinel = 987654321.25;
  let unknownFrames = 0, numericFeatures = 0;
  const markFrames = (value) => {
    if (!value || typeof value !== 'object') return;
    if (typeof value.status === 'string' && value.features && typeof value.features === 'object') {
      value.status = 'unknown';
      value.reason = 'missing_latest_closed';
      value.metadata = { ...(value.metadata || {}), unknown_reason: 'missing_latest_closed' };
      unknownFrames += 1;
      const poisonFeatures = (featureValue) => {
        if (!featureValue || typeof featureValue !== 'object') return;
        for (const [key, child] of Object.entries(featureValue)) {
          if (typeof child === 'number') {
            featureValue[key] = sentinel;
            numericFeatures += 1;
          } else {
            poisonFeatures(child);
          }
        }
      };
      poisonFeatures(value.features);
    }
    for (const child of Object.values(value)) {
      if (child !== value.features && child !== value.metadata) {
        markFrames(child);
      }
    }
  };
  markFrames(missing);
  assert.equal(unknownFrames > 0, true, 'fixture must contain timeframe frames');
  assert.equal(numericFeatures > 0, true, 'fixture must contain numeric indicators');
  env.enqueue(missing);
  const panel = env.document.getElementById('timeframeResearch');
  panel.open = true;
  panel.dispatchEvent({ type: 'toggle' });
  await env.ready();
  const treeText = (node) => [node?.textContent || '', ...[...(node?.children || [])].map(treeText)].join(' ');
  const rendered = treeText(panel);
  assert.match(rendered, /缺最新已收盤 bar/);
  assert.doesNotMatch(rendered.replace(/[,\s]/g, ''), /987654321/);
});

test('polls pending snapshots with bounded GET backoff and auto-refreshes only the selected symbol at 60 seconds', async () => {
  const env = makeEnvironment();
  await env.ready();
  const panel = env.document.getElementById('timeframeResearch');
  env.enqueue(unknownPacket());
  panel.open = true;
  panel.dispatchEvent({ type: 'toggle' });
  await env.ready();
  const pending = packet({ symbol: 'DEMO2USDT', status: 'pending', reason: 'pending' });
  pending.sample_frames = {};
  env.enqueue(pending);
  env.document.getElementById('timeframeSymbol').value = 'DEMO2USDT';
  env.document.getElementById('timeframeView').dispatchEvent({ type: 'click' });
  await env.ready();
  const selectedAt = env.calls[1].at;
  assert.equal(env.timers.size, 1, 'only one scheduler timer may remain');
  assert.equal([...env.timers.values()][0].delay, 2000);

  env.enqueue(packet({ symbol: 'DEMO2USDT' }));
  await env.runNextTimer();
  assert.deepEqual({ url: env.calls[2].url, method: env.calls[2].method }, { url: '/api/analysis/timeframes', method: 'GET' });
  assert.equal(env.calls[2].url.includes('?'), false);
  assert.equal(env.timers.size, 1);
  const autoTimer = [...env.timers.values()][0];
  assert.equal(autoTimer.due - selectedAt >= 60000, true);

  env.enqueue(packet({ symbol: 'DEMO2USDT' }));
  await env.runNextTimer();
  assert.deepEqual({ url: env.calls[3].url, method: env.calls[3].method }, { url: '/api/analysis/select', method: 'POST' });
  assert.deepEqual(JSON.parse(env.calls[3].body), { symbol: 'DEMO2USDT' });
  assert.equal(env.calls.filter((call) => call.method === 'POST').every((call) => JSON.parse(call.body).symbol === 'DEMO2USDT'), true);
  assert.equal(env.timers.size, 1);
});

test('pauses while hidden, masks expired content, and ignores late responses without leaking symbols', async () => {
  const env = makeEnvironment();
  await env.ready();
  const panel = env.document.getElementById('timeframeResearch');
  env.enqueue(unknownPacket());
  panel.open = true;
  panel.dispatchEvent({ type: 'toggle' });
  await env.ready();

  let resolveLate;
  env.enqueueRaw(new Promise((resolve) => { resolveLate = resolve; }));
  env.document.getElementById('timeframeSymbol').value = 'DEMO3USDT';
  env.document.getElementById('timeframeView').dispatchEvent({ type: 'click' });
  await env.ready();
  assert.equal(env.window.JEVTimeframeResearch._controller.isInFlight(), true);
  const beforeHidden = env.calls.length;
  env.dispatchVisibility(true);
  resolveLate(env.response(packet({ symbol: 'DEMO3USDT' })));
  await env.ready();
  assert.equal(env.calls.length, beforeHidden);
  assert.equal(env.document.body.textContent.includes('DEMO3USDT'), false);

  env.setLocked(true);
  env.dispatchVisibility(false);
  await env.ready();
  assert.equal(env.window.JEVTimeframeResearch._controller.selectedSymbol(), '');
  assert.equal(env.document.body.textContent.includes('DEMO3USDT'), false);
  assert.match(env.document.getElementById('timeframeResearch').textContent, /研究內容已遮蔽/);
  assert.equal(env.timers.size, 0);
  assert.equal(env.calls.length, beforeHidden);
});

test('validates symbols without URLs, headers, storage, or an eleventh timeframe alias', async () => {
  const env = makeEnvironment();
  await env.ready();
  const api = env.window.JEVTimeframeResearch._test;
  assert.equal(api.validateSymbol('DEMO12USDT', 'DEMO'), 'DEMO12USDT');
  assert.equal(api.validateSymbol('DEMO13USDT', 'DEMO'), null);
  assert.equal(api.validateSymbol('https://example.invalid/BTCUSDT', 'PUBLIC_BINANCE'), null);
  assert.equal(api.validateSymbol('BTCUSDT', 'PUBLIC_BINANCE'), 'BTCUSDT');
  assert.equal(/localStorage|sessionStorage|localhost:8766|127\.0\.0\.1:8766|innerHTML\s*=/.test(SOURCE), false);
  assert.equal(/\/api\/analysis\/timeframes\?/.test(SOURCE), false);
  assert.equal(/['"]60m['"]/.test(SOURCE), false);
  assert.equal(env.window.JEVTimeframeResearch._test.REQUEST_TIMEOUT_MS <= 4000, true);
  assert.equal(env.window.JEVTimeframeResearch._test.AUTO_REFRESH_MS >= 60000, true);
  assert.equal(env.window.JEVTimeframeResearch._test.FAILURE_COOLDOWN_MS >= 60000, true);
});

(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      process.stdout.write(`ok - ${name}\n`);
    } catch (error) {
      failed += 1;
      process.stderr.write(`not ok - ${name}\n${error.stack || error}\n`);
    }
  }
  process.stdout.write(`${tests.length - failed}/${tests.length} tests passed\n`);
  if (failed) process.exitCode = 1;
})();
