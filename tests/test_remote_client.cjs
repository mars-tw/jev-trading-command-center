const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const sourcePath = process.argv[2] || path.resolve(__dirname, '../web/remote-client.js');

class ClassList {
  constructor(el) { this.el = el; }
  list() { return this.el.className.split(/\s+/).filter(Boolean); }
  add(...names) { this.el.className = [...new Set([...this.list(), ...names])].join(' '); }
  contains(name) { return this.list().includes(name); }
}

class Element {
  constructor(tag, doc) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.ownerDocument = doc; this.children = []; this.parentNode = null;
    this.id = ''; this.className = ''; this.dataset = {}; this.attributes = {};
    this.style = {}; this.disabled = false; this.hidden = false; this.inert = false;
    this.listeners = {}; this._text = ''; this.title = ''; this.classList = new ClassList(this);
  }
  set textContent(v) { this._text = String(v ?? ''); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  appendChild(el) { el.parentNode = this; this.children.push(el); return el; }
  removeChild(el) { this.children = this.children.filter(x => x !== el); el.parentNode = null; return el; }
  setAttribute(k, v) {
    v = String(v); this.attributes[k] = v;
    if (k === 'id') this.id = v; if (k === 'class') this.className = v;
    if (k === 'hidden') this.hidden = true; if (k === 'inert') this.inert = true;
    if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
  }
  getAttribute(k) { if (k === 'id') return this.id || null; if (k === 'class') return this.className || null; return Object.hasOwn(this.attributes, k) ? this.attributes[k] : null; }
  hasAttribute(k) { return this.getAttribute(k) !== null; }
  removeAttribute(k) { delete this.attributes[k]; if (k === 'hidden') this.hidden = false; if (k === 'inert') this.inert = false; }
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
  matches(selector) {
    if (selector === '*') return true;
    const attr = selector.match(/^\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]$/);
    if (attr) return this.hasAttribute(attr[1]) && (attr[2] === undefined || this.getAttribute(attr[1]) === attr[2]);
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(x => x.trim()); const out = [];
    const walk = node => node.children.forEach(child => { if (selectors.some(s => child.matches(s))) out.push(child); walk(child); });
    walk(this); return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let n = this; n; n = n.parentNode) if (n.matches(selector)) return n; return null; }
}

function response(status, body, malformed = false) {
  return {status, ok: status >= 200 && status < 300, json: async () => { if (malformed) throw Error('bad json'); return body; }};
}

let activeObserver = null;
function environment(queue) {
  const listeners = {};
  const doc = {
    listeners: {},
    createElement(tag) { return new Element(tag, this); },
    addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); },
    querySelectorAll(s) { return this.body.querySelectorAll(s); },
    getElementById(id) { return this.documentElement.querySelector('#' + id); }
  };
  doc.documentElement = new Element('html', doc); doc.head = new Element('head', doc); doc.body = new Element('body', doc);
  doc.documentElement.appendChild(doc.head); doc.documentElement.appendChild(doc.body);
  const privatePanel = doc.createElement('main'); privatePanel.id = 'private'; doc.body.appendChild(privatePanel);
  const originallyHidden = doc.createElement('section'); originallyHidden.id = 'originalHidden'; originallyHidden.setAttribute('aria-hidden', 'false'); originallyHidden.setAttribute('inert', ''); doc.body.appendChild(originallyHidden);
  const calls = [], redirects = [], timers = new Map(); let timerId = 0;
  class HeadersMock { constructor(init = {}) { this.values = {...init}; } set(k, v) { this.values[k] = v; } }
  class MutationObserverMock { constructor(fn) { this.fn = fn; activeObserver = this; } observe() {} }
  const fetch = async (url, init) => {
    calls.push([url, init]); const next = queue.shift();
    if (next instanceof Error) throw next; if (typeof next === 'function') return next(url, init); return next;
  };
  const win = {
    document: doc, fetch, Headers: HeadersMock, Request: class Request {}, URL, MutationObserver: MutationObserverMock,
    location: {href: 'https://jev.example/stage', origin: 'https://jev.example', replace: x => redirects.push(x)},
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); },
    setInterval(fn) { timers.set(++timerId, fn); return timerId; }, clearInterval(id) { timers.delete(id); },
    requestAnimationFrame(fn) { timers.set(++timerId, fn); return timerId; }, cancelAnimationFrame(id) { timers.delete(id); },
    addEventListener(k, fn) { (listeners[k] ||= []).push(fn); }, console
  };
  win.window = win; win.self = win; win.globalThis = win;
  vm.createContext(win); vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), win, {filename: sourcePath});
  return {win, doc, calls, redirects, privatePanel, originallyHidden, listeners, mutation: () => activeObserver?.fn([])};
}

async function settle() { await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); }

async function main() {
  const cases = [];
  async function test(name, fn) { try { await fn(); cases.push({name, pass: true}); } catch (error) { cases.push({name, pass: false, message: error.message}); } }

  await test('initial content is inert before auth and prior hidden states are restored exactly', async () => {
    let release; const pending = new Promise(resolve => { release = resolve; });
    const e = environment([() => pending]);
    assert.equal(e.doc.documentElement.getAttribute('data-jev-privacy'), 'checking');
    assert.equal(e.privatePanel.inert, true); assert.equal(e.privatePanel.getAttribute('aria-hidden'), 'true');
    release(response(200, {ok: true, csrf: 'token', read_only: false})); await settle();
    assert.equal(e.win.JEVRemote.isLocked(), false); assert.equal(e.doc.documentElement.hasAttribute('data-jev-privacy'), false);
    assert.equal(e.privatePanel.inert, false); assert.equal(e.privatePanel.hasAttribute('aria-hidden'), false);
    assert.equal(e.originallyHidden.inert, true); assert.equal(e.originallyHidden.getAttribute('aria-hidden'), 'false');
  });

  await test('network non-200 malformed and missing-CSRF bootstrap all fail closed', async () => {
    for (const first of [response(503, {}), response(200, {}, true), response(200, {ok: true, csrf: ''}), new Error('offline')]) {
      const e = environment([first]); await settle();
      assert.equal(e.win.JEVRemote.isLocked(), true); assert.equal(e.doc.documentElement.getAttribute('data-jev-privacy'), 'locked');
      assert.equal(e.doc.getElementById('jevRemoteLock').dataset.state, 'locked');
    }
  });

  await test('same-origin application fetch waits for auth while cross-origin stays untouched', async () => {
    let release; const auth = new Promise(resolve => { release = resolve; });
    const e = environment([() => auth, response(200, {public: true}), response(200, {status: 'ok'})]);
    const local = e.win.fetch('/api/status'); const external = e.win.fetch('https://public.example/info');
    await settle(); assert.equal(e.calls.length, 2); assert.equal(e.calls[1][0], 'https://public.example/info');
    release(response(200, {ok: true, csrf: 'csrf', read_only: false})); await settle(); await local; await external;
    assert.equal(e.calls[2][0], '/api/status');
  });

  await test('a 401 masks unknown future nodes and permanently disables their controls', async () => {
    const e = environment([response(200, {ok: true, csrf: 'x'}), response(401, {})]); await settle();
    await e.win.fetch('/api/status'); assert.equal(e.win.JEVRemote.isLocked(), true);
    const future = e.doc.createElement('section'); future.id = 'futureHud'; future.textContent = 'private future data';
    const button = e.doc.createElement('button'); future.appendChild(button); e.doc.body.appendChild(future); e.mutation();
    assert.equal(future.inert, true); assert.equal(future.getAttribute('aria-hidden'), 'true'); assert.equal(button.disabled, true);
    assert.equal(e.doc.documentElement.getAttribute('data-jev-privacy'), 'locked');
  });

  await test('logout requires HTTP success and explicit logged_out receipt', async () => {
    for (const reply of [response(403, {}), response(200, {logged_out: false}), new Error('network')]) {
      const e = environment([response(200, {ok: true, csrf: 'x'}), reply]); await settle();
      assert.equal(await e.win.JEVRemote.logout(), false); assert.equal(e.redirects.length, 0);
      assert.equal(e.doc.getElementById('jevRemoteLock').dataset.state, 'logout-unconfirmed');
    }
    const ok = environment([response(200, {ok: true, csrf: 'x'}), response(200, {logged_out: true})]); await settle();
    assert.equal(await ok.win.JEVRemote.logout(), true); assert.deepEqual(ok.redirects, ['/login']);
  });

  await test('pagehide only masks and pageshow freshly revalidates before reveal', async () => {
    const e = environment([response(200, {ok: true, csrf: 'one'}), response(200, {ok: true, csrf: 'two'})]); await settle();
    e.listeners.pagehide[0]({persisted: true});
    assert.equal(e.doc.documentElement.getAttribute('data-jev-privacy'), 'checking');
    assert.equal(e.calls.filter(x => x[0] === '/api/auth/logout').length, 0);
    e.listeners.pageshow[0]({persisted: true}); assert.equal(e.privatePanel.inert, true); await settle();
    assert.equal(e.doc.documentElement.hasAttribute('data-jev-privacy'), false);
    assert.equal(e.calls.filter(x => x[0] === '/api/auth/session').length, 2);
  });

  await test('read-only leaves view pet and motion controls usable', async () => {
    let release; const e = environment([() => new Promise(resolve => { release = resolve; })]);
    for (const id of ['btnPause', 'lwToggle', 'btnAnim', 'lwMotion', 'companionPet']) { const b = e.doc.createElement('button'); b.id = id; e.doc.body.appendChild(b); }
    release(response(200, {ok: true, csrf: 'x', read_only: true})); await settle(); e.mutation();
    assert.equal(e.doc.getElementById('btnPause').disabled, true); assert.equal(e.doc.getElementById('lwToggle').disabled, true);
    for (const id of ['btnAnim', 'lwMotion', 'companionPet']) assert.equal(e.doc.getElementById(id).disabled, false);
  });

  const failed = cases.filter(x => !x.pass);
  console.log(JSON.stringify({source: sourcePath, total: cases.length, passed: cases.length - failed.length, failed}, null, 2));
  if (failed.length) process.exitCode = 1;
}

main().catch(error => { console.error(error.stack); process.exitCode = 1; });
