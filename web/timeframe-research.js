/* Read-only multi-timeframe research companion. No order or native-tool authority. */
(function () {
  'use strict';

  const W = window;
  const D = document;
  if (W.JEVTimeframeResearch) return;

  const CANONICAL = Object.freeze([
    Object.freeze({ key: '5m', label: '5分鐘' }),
    Object.freeze({ key: '15m', label: '15分鐘' }),
    Object.freeze({ key: '30m', label: '30分鐘' }),
    Object.freeze({ key: '45m', label: '45分鐘' }),
    Object.freeze({ key: '1h', label: '60分鐘／1小時' }),
    Object.freeze({ key: '2h', label: '2小時' }),
    Object.freeze({ key: '4h', label: '4小時' }),
    Object.freeze({ key: '8h', label: '8小時' }),
    Object.freeze({ key: '16h', label: '16小時' }),
    Object.freeze({ key: '1d', label: '24小時' })
  ]);
  const FRAME_KEYS = new Set(CANONICAL.map((item) => item.key));
  const FRAME_STATUS = new Set(['ready', 'pending', 'unknown', 'disabled', 'failure']);
  const SOURCES = new Set(['DEMO', 'SYNTHETIC', 'PUBLIC_BINANCE']);
  const REASONS = new Set([
    'missing_source', 'not_selected', 'missing_latest', 'missing_latest_closed', 'missing_frame',
    'incomplete', 'insufficient_bars', 'invalid', 'invalid_symbol',
    'forbidden', 'disabled', 'pending', 'cache_miss', 'gap',
    'upstream_failure', 'fetch_failed', 'timeout', 'source_mismatch',
    'authority_mismatch', 'symbol_mismatch', 'unknown'
  ]);
  const PENDING_BACKOFF_MS = Object.freeze([2000, 5000, 10000, 30000, 60000]);
  const AUTO_REFRESH_MS = 60000;
  const FAILURE_COOLDOWN_MS = 60000;
  const REQUEST_TIMEOUT_MS = 3900;
  const MAX_PENDING_POLLS = PENDING_BACKOFF_MS.length;
  const API_SNAPSHOT = '/api/analysis/timeframes';
  const API_SELECT = '/api/analysis/select';

  const isRecord = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
  const isFiniteNumber = (value) => typeof value === 'number' && Number.isFinite(value);
  const finiteOrNull = (value) => isFiniteNumber(value) ? value : null;
  const integerOrNull = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const timestampOrNull = (value) => isFiniteNumber(value) && value >= 0 && value <= 8640000000000000
    ? Math.trunc(value) : null;
  const booleanOrNull = (value) => value === true ? true : value === false ? false : null;
  const safeToken = (value, length = 40) => typeof value === 'string' &&
    value.length > 0 && value.length <= length && /^[a-z0-9_:+.-]+$/i.test(value) ? value : null;
  const cleanText = (value, length = 100) => typeof value === 'string'
    ? value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim().slice(0, length) : '';

  function pageSource() {
    return W.__JEV_DEMO__ === true ? 'DEMO' : 'PUBLIC_BINANCE';
  }

  function normalizeSource(value) {
    if (!SOURCES.has(value)) return null;
    return value;
  }

  function validateSymbol(value, source = pageSource()) {
    if (typeof value !== 'string') return null;
    const symbol = value.trim().toUpperCase();
    if (source === 'DEMO' || source === 'SYNTHETIC') return /^DEMO(?:[1-9]|1[0-2])USDT$/.test(symbol) ? symbol : null;
    return /^(?=.{5,24}$)[A-Z0-9]+USDT$/.test(symbol) ? symbol : null;
  }

  function normalizeReason(value, fallback = 'unknown') {
    return REASONS.has(value) ? value : fallback;
  }

  function normalizeStatus(value, reason) {
    if (FRAME_STATUS.has(value)) return value;
    if (value === 'failed' || value === 'error') return 'failure';
    if (reason === 'disabled') return 'disabled';
    if (reason === 'pending') return 'pending';
    return 'unknown';
  }

  function normalizeCoverage(value) {
    const coverage = isRecord(value) ? value : {};
    return Object.freeze({
      firstOpenAt: timestampOrNull(coverage.first_open_at),
      lastClosedAt: timestampOrNull(coverage.last_closed_at),
      selectedBars: integerOrNull(coverage.selected_bars),
      contiguous: booleanOrNull(coverage.contiguous)
    });
  }

  function normalizeFeatures(value) {
    if (!isRecord(value)) return null;
    const numericKeys = ['ema20', 'ema50', 'macd', 'macd_signal', 'macd_hist', 'rsi14', 'atr', 'atr_pct'];
    const numeric = {};
    for (const key of numericKeys) {
      numeric[key] = finiteOrNull(value[key]);
      if (numeric[key] === null) return null;
    }
    const trend = safeToken(value.trend, 32);
    const breakout = safeToken(value.breakout, 32);
    const pattern = safeToken(value.pattern, 40);
    if (!trend || !breakout || !pattern) return null;
    return Object.freeze({
      price: finiteOrNull(value.price),
      ema20: numeric.ema20,
      ema50: numeric.ema50,
      macd: numeric.macd,
      macdSignal: numeric.macd_signal,
      macdHist: numeric.macd_hist,
      rsi14: numeric.rsi14,
      atr: numeric.atr,
      atrPct: numeric.atr_pct,
      trend,
      breakout,
      pattern
    });
  }

  function staleFromAsOf(actualAsOf, expectedAsOf, explicitStale) {
    if (explicitStale === true) return true;
    if (actualAsOf === null || expectedAsOf === null) return null;
    return actualAsOf !== expectedAsOf;
  }

  function normalizeFrame(value, key, source, fallbackStatus = 'unknown', fallbackReason = 'unknown') {
    if (!FRAME_KEYS.has(key)) return null;
    if (!isRecord(value)) {
      return Object.freeze({
        key,
        status: fallbackStatus === 'ready' ? 'unknown' : fallbackStatus,
        reason: fallbackStatus === 'ready' ? 'missing_frame' : fallbackReason,
        source,
        sourceInterval: null,
        metadata: null,
        features: null,
        dataStale: null
      });
    }
    const metadataRaw = isRecord(value.metadata) ? value.metadata : {};
    let reason = normalizeReason(metadataRaw.unknown_reason || value.reason, fallbackReason);
    let status = normalizeStatus(value.status, reason);
    const actualAsOf = timestampOrNull(metadataRaw.actual_asof);
    const expectedAsOf = timestampOrNull(metadataRaw.expected_asof);
    const lastClosedAt = timestampOrNull(metadataRaw.last_closed_at);
    const features = normalizeFeatures(value.features);
    const preventsIndicators = new Set(['missing_latest', 'missing_latest_closed', 'incomplete', 'insufficient_bars', 'invalid', 'gap']);
    if (status === 'ready' && (!features || preventsIndicators.has(reason))) {
      status = 'unknown';
      reason = reason === 'unknown' ? 'incomplete' : reason;
    }
    if (status === 'ready') {
      const integralTimestamp = (value) => Number.isInteger(value) && value >= 0 && value <= 8640000000000000;
      if (!integralTimestamp(metadataRaw.actual_asof)
          || !integralTimestamp(metadataRaw.expected_asof)
          || !integralTimestamp(metadataRaw.last_closed_at)) {
        status = 'unknown';
        reason = 'invalid';
      } else if (metadataRaw.actual_asof !== metadataRaw.expected_asof) {
        status = 'unknown';
        reason = 'missing_latest_closed';
      } else if (metadataRaw.last_closed_at !== metadataRaw.actual_asof) {
        status = 'unknown';
        reason = 'invalid';
      }
    }
    const metadata = Object.freeze({
      inputCount: integerOrNull(metadataRaw.input_count),
      sourceClosedCount: integerOrNull(metadataRaw.source_closed_count),
      closedCount: integerOrNull(metadataRaw.closed_count),
      coverage: normalizeCoverage(metadataRaw.coverage),
      actualAsOf,
      expectedAsOf,
      lastClosedAt,
      analyzedAt: timestampOrNull(metadataRaw.analyzed_at),
      serverCutoff: timestampOrNull(metadataRaw.server_cutoff),
      fetchedAt: timestampOrNull(metadataRaw.fetched_at)
    });
    return Object.freeze({
      key,
      status,
      reason,
      source,
      sourceInterval: safeToken(metadataRaw.source_interval, 12),
      metadata,
      features: status === 'ready' ? features : null,
      dataStale: staleFromAsOf(actualAsOf, expectedAsOf, metadataRaw.stale)
    });
  }

  function normalizeConnector(value) {
    const connector = isRecord(value) ? value : {};
    const capability = isRecord(connector.capability) ? connector.capability : {};
    const enabled = connector.enabled === true;
    const frameCount = integerOrNull(connector.frame_count);
    const forbiddenProof = connector.spawned === true ||
      connector.research_request_enabled === true ||
      connector.native_invocation_available === true ||
      capability.capability_verified === true ||
      capability.spawn_allowed === true ||
      capability.tools_allowed === true ||
      capability.mcp_allowed === true ||
      capability.plugins_allowed === true;
    return Object.freeze({
      displayName: 'Antigravity',
      protocolId: 'AGY',
      enabled,
      compatibilityVersion: connector.version === '1.4.2' ? '1.4.2' : null,
      status: enabled ? 'capability_unverified' : 'disabled',
      contextReady: connector.context_ready === true && frameCount === CANONICAL.length,
      frameCount,
      researchRequestEnabled: false,
      nativeInvocationAvailable: false,
      spawned: false,
      model: 'UNVERIFIED',
      proofContradiction: forbiddenProof
    });
  }

  function unknownPacket(reason, expectedSource = pageSource()) {
    const status = reason === 'disabled' ? 'disabled'
      : ['fetch_failed', 'upstream_failure', 'timeout'].includes(reason) ? 'failure' : 'unknown';
    const frames = {};
    for (const item of CANONICAL) frames[item.key] = normalizeFrame(null, item.key, expectedSource, status, reason);
    return Object.freeze({
      valid: false,
      tainted: reason === 'source_mismatch' || reason === 'authority_mismatch' || reason === 'symbol_mismatch',
      source: expectedSource,
      symbol: null,
      status,
      reason,
      analysisOnly: true,
      executionAuthority: false,
      analyzedAt: null,
      serverCutoff: null,
      serverTime: null,
      fetchedAt: null,
      stale: null,
      dataStale: null,
      cacheStale: null,
      cacheTtlMs: null,
      cacheAgeMs: null,
      frames: Object.freeze(frames),
      connector: normalizeConnector(null)
    });
  }

  function normalizePacket(value, expectedSource = pageSource(), expectedSymbol = null) {
    if (!isRecord(value)) return unknownPacket('invalid', expectedSource);
    const source = normalizeSource(value.source);
    const sourceMatchesPage = expectedSource === 'DEMO'
      ? source === 'DEMO' || source === 'SYNTHETIC'
      : source === 'PUBLIC_BINANCE';
    if (!source || !sourceMatchesPage) return unknownPacket('source_mismatch', expectedSource);
    if (value.analysis_only !== true || value.execution_authority !== false) {
      return unknownPacket('authority_mismatch', expectedSource);
    }
    const symbol = value.symbol === null || value.symbol === undefined ? null : validateSymbol(value.symbol, source);
    if (value.symbol !== null && value.symbol !== undefined && !symbol) return unknownPacket('invalid_symbol', expectedSource);
    if (expectedSymbol && symbol !== expectedSymbol) return unknownPacket('symbol_mismatch', expectedSource);
    const reason = normalizeReason(value.reason, value.status === 'ready' ? 'unknown' : 'unknown');
    const status = normalizeStatus(value.status, reason);
    const rawFrames = isRecord(value.frames) ? value.frames : isRecord(value.sample_frames) ? value.sample_frames : {};
    const frames = {};
    for (const item of CANONICAL) frames[item.key] = normalizeFrame(rawFrames[item.key], item.key, source, status, reason);
    return Object.freeze({
      valid: true,
      tainted: false,
      source,
      symbol,
      status,
      reason,
      analysisOnly: true,
      executionAuthority: false,
      analyzedAt: timestampOrNull(value.analyzed_at),
      serverCutoff: timestampOrNull(value.server_cutoff),
      serverTime: timestampOrNull(value.server_time),
      fetchedAt: timestampOrNull(value.fetched_at),
      stale: booleanOrNull(value.stale),
      dataStale: booleanOrNull(value.data_stale),
      cacheStale: booleanOrNull(value.cache_stale),
      cacheTtlMs: isFiniteNumber(value.cache_ttl_ms) && value.cache_ttl_ms >= 0 ? value.cache_ttl_ms : null,
      cacheAgeMs: isFiniteNumber(value.cache_age_ms) && value.cache_age_ms >= 0 ? value.cache_age_ms : null,
      frames: Object.freeze(frames),
      connector: normalizeConnector(value.antigravity)
    });
  }

  function formatNumber(value, options) {
    if (!isFiniteNumber(value)) return '—';
    const opts = isRecord(options) ? options : {};
    const absolute = Math.abs(value);
    let maximumFractionDigits = absolute >= 1000 ? 2 : absolute >= 1 ? 4 : absolute >= 0.01 ? 6 : 8;
    if (Number.isSafeInteger(opts.maximumFractionDigits)) maximumFractionDigits = opts.maximumFractionDigits;
    try {
      return new Intl.NumberFormat('zh-Hant', {
        useGrouping: true,
        minimumFractionDigits: 0,
        maximumFractionDigits
      }).format(value);
    } catch (_) {
      return value.toFixed(maximumFractionDigits).replace(/\.?0+$/, '');
    }
  }

  function formatUtc(value) {
    const timestamp = timestampOrNull(value);
    if (timestamp === null) return '—';
    try {
      return new Date(timestamp).toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
    } catch (_) {
      return '—';
    }
  }

  function truth(value) {
    return value === true ? '是' : value === false ? '否' : '—';
  }

  function statusLabel(status) {
    return ({ ready: '就緒', pending: '準備中', unknown: '資料不足', disabled: '未啟用', failure: '失敗' })[status] || '資料不足';
  }

  function reasonLabel(reason) {
    return ({
      missing_source: '尚未選擇研究標的',
      not_selected: '尚未選擇研究標的',
      missing_latest: '缺最新已收盤 bar',
      missing_latest_closed: '缺最新已收盤 bar',
      missing_frame: '缺少此時間週期',
      incomplete: '資料不足',
      insufficient_bars: '資料不足',
      invalid: '資料格式無效',
      invalid_symbol: '標的格式無效',
      forbidden: '此研究請求不允許',
      disabled: '研究功能未啟用',
      pending: '研究資料準備中',
      cache_miss: '尚無快照',
      gap: '已收盤資料不連續',
      upstream_failure: '公開行情來源暫時失敗',
      fetch_failed: '研究資料取得失敗',
      timeout: '研究請求逾時',
      source_mismatch: '資料來源不符，已停止顯示',
      authority_mismatch: '權限標記不符，已停止顯示',
      symbol_mismatch: '回傳標的不符，已停止顯示',
      unknown: '資料不足'
    })[reason] || '資料不足';
  }

  function enumLabel(kind, value) {
    const labels = {
      trend: { up: '上行', down: '下行', sideways: '橫向', flat: '持平', mixed: '混合', none: '無' },
      breakout: { up: '向上', down: '向下', none: '無' },
      pattern: {
        none: '無', doji: '十字', hammer: '錘形', shooting_star: '流星',
        bullish_engulfing: '多方吞噬', bearish_engulfing: '空方吞噬'
      }
    };
    return labels[kind] && labels[kind][value] ? labels[kind][value] : value;
  }

  function element(tag, className, text) {
    const item = D.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function replaceText(node, value) {
    const text = String(value);
    if (node.textContent !== text) node.textContent = text;
  }

  function addFact(parent, label, value) {
    const row = element('div', 'timeframe-research__fact');
    row.append(element('dt', '', label), element('dd', '', value));
    parent.appendChild(row);
  }

  function insertAfterMain(panel) {
    const main = D.querySelector('main');
    if (main && typeof main.insertAdjacentElement === 'function') {
      main.insertAdjacentElement('afterend', panel);
    } else if (main && main.parentNode) {
      main.parentNode.insertBefore(panel, main.nextSibling || null);
    } else {
      D.body.appendChild(panel);
    }
  }

  function createPanel() {
    const panel = element('details', 'timeframe-research');
    panel.id = 'timeframeResearch';
    panel.setAttribute('aria-label', '多週期研究');
    panel.open = false;

    const summary = element('summary', 'timeframe-research__summary');
    const summaryCopy = element('span', 'timeframe-research__summary-copy');
    summaryCopy.append(element('b', '', '多週期研究'), element('small', '', '10 個已收盤時間週期 · 僅供研究'));
    const summaryState = element('span', 'timeframe-research__summary-state', '尚未檢視');
    summary.append(summaryCopy, summaryState);

    const body = element('div', 'timeframe-research__body');
    const notice = element('p', 'timeframe-research__notice', '研究資料不是交易或下單授權；不產生策略、投票、勝率或獲利宣告。');
    const controls = element('div', 'timeframe-research__controls');
    const field = element('label', 'timeframe-research__field');
    const fieldText = element('span', '', '研究標的');
    const input = element('input', 'timeframe-research__input');
    input.id = 'timeframeSymbol';
    input.type = 'text';
    input.autocomplete = 'off';
    input.autocapitalize = 'characters';
    input.spellcheck = false;
    input.maxLength = 24;
    input.setAttribute('aria-describedby', 'timeframeSymbolHint');
    const datalist = element('datalist');
    datalist.id = 'timeframeDemoSymbols';
    if (pageSource() === 'DEMO') {
      input.setAttribute('list', datalist.id);
      input.placeholder = '選擇 DEMO1USDT…DEMO12USDT';
      for (let index = 1; index <= 12; index += 1) {
        const option = element('option');
        option.value = `DEMO${index}USDT`;
        datalist.appendChild(option);
      }
    } else {
      input.placeholder = '例如 BTCUSDT';
    }
    const hint = element('small', '', pageSource() === 'DEMO'
      ? 'DEMO 僅接受 12 個合成標的；不會連外。' : '只接受公開 USDT 標的，不接受網址。');
    hint.id = 'timeframeSymbolHint';
    field.append(fieldText, input, datalist, hint);
    const buttonBox = element('div', 'timeframe-research__buttons');
    const viewButton = element('button', 'timeframe-research__primary', '檢視研究');
    viewButton.id = 'timeframeView';
    viewButton.type = 'button';
    const refreshButton = element('button', '', '更新');
    refreshButton.id = 'timeframeRefresh';
    refreshButton.type = 'button';
    refreshButton.disabled = true;
    buttonBox.append(viewButton, refreshButton);
    controls.append(field, buttonBox);

    const status = element('div', 'timeframe-research__status', '尚未選擇研究標的');
    status.id = 'timeframeStatus';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const metadata = element('dl', 'timeframe-research__metadata');
    metadata.id = 'timeframeMetadata';
    const connector = element('section', 'timeframe-research__connector');
    connector.id = 'timeframeConnector';
    connector.setAttribute('aria-label', 'Antigravity 研究連接器');
    const grid = element('div', 'timeframe-research__grid');
    grid.id = 'timeframeGrid';
    const foot = element('p', 'timeframe-research__foot', 'TF 在此代表時間週期（timeframe），不是 TensorFlow。');
    body.append(notice, controls, status, metadata, connector, grid, foot);
    panel.append(summary, body);
    insertAfterMain(panel);
    return { panel, body, summaryState, input, viewButton, refreshButton, status, metadata, connector, grid };
  }

  function crewEvidence() {
    const readback = W.__companionReadback;
    if (!isRecord(readback) || readback.ready !== true || !Array.isArray(readback.crew)) {
      return Object.freeze({ native: false, pid: null, job: '' });
    }
    const worker = readback.crew.find((item) => isRecord(item) && item.alias === 'AGY');
    const pid = worker && Number.isSafeInteger(worker.pid) && worker.pid > 0 ? worker.pid : null;
    const job = worker ? cleanText(worker.job, 72) : '';
    return Object.freeze({ native: !!(pid && job && worker.provenance === 'UNVERIFIED'), pid, job });
  }

  function renderConnector(node, connector, source) {
    node.replaceChildren();
    const title = element('h3', '', 'Antigravity · AGY');
    const config = element('p', '', connector.enabled
      ? `連接器設定：已啟用 · 相容設定 ${connector.compatibilityVersion || '未驗證'}（不是上線或推論證明）`
      : '連接器設定：未啟用（不是上線或推論證明）');
    const context = element('p', '', connector.contextReady
      ? source === 'DEMO' || source === 'SYNTHETIC'
        ? `上下文：已準備 ${connector.frameCount} 個 DEMO 合成已收盤摘要`
        : `上下文：已準備 ${connector.frameCount} 個公開行情已收盤摘要`
      : `上下文：未完成（${connector.frameCount === null ? '—' : connector.frameCount}/10）`);
    const capability = element('p', '', '新研究連接器：能力 UNVERIFIED · 研究請求停用 · spawned false · 原生呼叫／工具／MCP／外掛證明均為 false');
    if (connector.proofContradiction) capability.textContent = '新研究連接器：證明互相矛盾，已失敗關閉；模型仍為 UNVERIFIED，且不發出原生呼叫';
    const crew = crewEvidence();
    const crewLine = element('p', '', crew.native
      ? `既有夥伴 HUD 回讀：原生 PID ${crew.pid} · job ${crew.job} · 新鮮心跳已由既有 HUD 驗證 · 模型 UNVERIFIED`
      : '既有夥伴 HUD 回讀：無已驗證原生 PID／job／新鮮心跳 · 模型 UNVERIFIED');
    node.append(title, config, context, capability, crewLine);
  }

  function renderMetadata(node, packet) {
    node.replaceChildren();
    addFact(node, '標的', packet.symbol || '—');
    addFact(node, '來源', packet.source);
    addFact(node, '分析時間 analyzed', formatUtc(packet.analyzedAt));
    addFact(node, '伺服器截點 server cutoff', formatUtc(packet.serverCutoff));
    addFact(node, '伺服器時間', formatUtc(packet.serverTime));
    addFact(node, '擷取時間 fetched_at', formatUtc(packet.fetchedAt));
    addFact(node, 'stale', truth(packet.stale));
    addFact(node, 'data_stale', truth(packet.dataStale));
    addFact(node, 'cache_stale', truth(packet.cacheStale));
    addFact(node, 'cache TTL', packet.cacheTtlMs === null ? '—' : `${formatNumber(packet.cacheTtlMs, { maximumFractionDigits: 0 })} ms`);
    addFact(node, 'cache age', packet.cacheAgeMs === null ? '—' : `${formatNumber(packet.cacheAgeMs, { maximumFractionDigits: 0 })} ms`);
  }

  function renderFrame(spec, frame) {
    const card = element('article', `timeframe-research__card is-${frame.status}`);
    card.dataset.interval = spec.key;
    card.dataset.status = frame.status;
    const head = element('div', 'timeframe-research__card-head');
    const heading = element('h3', '', spec.label);
    heading.appendChild(element('small', '', ` ${spec.key}`));
    head.append(heading, element('span', 'timeframe-research__badge', statusLabel(frame.status)));
    const source = element('p', 'timeframe-research__source', `來源：${frame.source}${frame.sourceInterval ? ` · 原始週期 ${frame.sourceInterval}` : ''}`);
    card.append(head, source);
    if (frame.metadata) {
      const timing = element('dl', 'timeframe-research__card-facts');
      addFact(timing, 'actual_asof', formatUtc(frame.metadata.actualAsOf));
      addFact(timing, 'expected_asof', formatUtc(frame.metadata.expectedAsOf));
      addFact(timing, 'last closed', formatUtc(frame.metadata.lastClosedAt));
      addFact(timing, 'analyzed', formatUtc(frame.metadata.analyzedAt));
      addFact(timing, 'server cutoff', formatUtc(frame.metadata.serverCutoff));
      addFact(timing, 'fetched_at', formatUtc(frame.metadata.fetchedAt));
      addFact(timing, '資料 stale', truth(frame.dataStale));
      const coverage = frame.metadata.coverage;
      const coverageText = coverage.selectedBars === null ? '—'
        : `${coverage.selectedBars} bars · ${coverage.contiguous === true ? '連續' : coverage.contiguous === false ? '不連續' : '連續性未提供'}`;
      addFact(timing, 'coverage', coverageText);
      addFact(timing, 'coverage 範圍', `${formatUtc(coverage.firstOpenAt)} → ${formatUtc(coverage.lastClosedAt)}`);
      addFact(timing, '輸入／已收盤／彙整', `${frame.metadata.inputCount === null ? '—' : frame.metadata.inputCount} / ${frame.metadata.sourceClosedCount === null ? '—' : frame.metadata.sourceClosedCount} / ${frame.metadata.closedCount === null ? '—' : frame.metadata.closedCount}`);
      card.appendChild(timing);
    }
    if (frame.status !== 'ready' || !frame.features) {
      card.appendChild(element('p', 'timeframe-research__missing', reasonLabel(frame.reason)));
      card.appendChild(element('p', 'timeframe-research__withheld', '指標不顯示；不產生決策。'));
      return card;
    }
    const features = frame.features;
    const indicators = element('dl', 'timeframe-research__indicators');
    if (features.price !== null) addFact(indicators, '已收盤價', formatNumber(features.price));
    addFact(indicators, 'EMA20／EMA50', `${formatNumber(features.ema20)} / ${formatNumber(features.ema50)}`);
    addFact(indicators, 'MACD／signal／hist', `${formatNumber(features.macd)} / ${formatNumber(features.macdSignal)} / ${formatNumber(features.macdHist)}`);
    addFact(indicators, 'RSI14', formatNumber(features.rsi14, { maximumFractionDigits: 2 }));
    addFact(indicators, 'ATR／ATR%', `${formatNumber(features.atr)} / ${formatNumber(features.atrPct, { maximumFractionDigits: 3 })}%`);
    addFact(indicators, '趨勢', enumLabel('trend', features.trend));
    addFact(indicators, '突破', enumLabel('breakout', features.breakout));
    addFact(indicators, '型態', enumLabel('pattern', features.pattern));
    card.appendChild(indicators);
    return card;
  }

  function renderPacket(refs, packet) {
    refs.panel.dataset.packetStatus = packet.status;
    replaceText(refs.summaryState, `${packet.symbol || '未選擇'} · ${statusLabel(packet.status)}`);
    replaceText(refs.status, packet.status === 'ready' && packet.symbol
      ? `${packet.symbol} 的 10 週期研究快照；每張卡片各自標示來源與已收盤時間。`
      : reasonLabel(packet.reason));
    renderMetadata(refs.metadata, packet);
    renderConnector(refs.connector, packet.connector, packet.source);
    refs.grid.replaceChildren();
    for (const spec of CANONICAL) refs.grid.appendChild(renderFrame(spec, packet.frames[spec.key]));
  }

  function start() {
    if (D.getElementById('timeframeResearch')) return;
    const refs = createPanel();
    const state = {
      selectedSymbol: '',
      explicitSelection: false,
      packet: null,
      inFlight: null,
      requestSerial: 0,
      timer: 0,
      nextAutoAt: 0,
      nextPendingAt: 0,
      pendingPolls: 0,
      failureCooldownUntil: 0,
      loadedOnce: false,
      eventPaused: false,
      terminalLocked: false
    };
    const reducedMotion = typeof W.matchMedia === 'function'
      ? W.matchMedia('(prefers-reduced-motion: reduce)') : null;

    function remoteLocked() {
      try {
        return !!(W.JEVRemote && typeof W.JEVRemote.isLocked === 'function' && W.JEVRemote.isLocked());
      } catch (_) {
        return true;
      }
    }

    function automationPaused() {
      const body = D.body;
      return state.terminalLocked || D.hidden || !refs.panel.open || remoteLocked() || state.eventPaused ||
        !!(reducedMotion && reducedMotion.matches) || !!(body && body.classList &&
          (body.classList.contains('noanim') || body.classList.contains('is-paused'))) ||
        W.__JEV_GLOBAL_PAUSED__ === true || W.__vizPaused === true;
    }

    function clearSchedule() {
      if (state.timer) W.clearTimeout(state.timer);
      state.timer = 0;
    }

    function abortInFlight() {
      if (state.inFlight && state.inFlight.controller) {
        state.inFlight.cancelled = true;
        state.inFlight.controller.abort();
      }
    }

    function maskForPrivacy(message) {
      if (state.terminalLocked) return;
      state.terminalLocked = true;
      state.requestSerial += 1;
      clearSchedule();
      abortInFlight();
      state.inFlight = null;
      state.packet = null;
      state.selectedSymbol = '';
      state.explicitSelection = false;
      refs.input.value = '';
      refs.panel.dataset.locked = 'true';
      refs.body.replaceChildren(element('p', 'timeframe-research__locked', message || '工作階段已鎖定；研究內容已遮蔽。'));
      replaceText(refs.summaryState, '已鎖定');
    }

    function checkPrivacy() {
      if (remoteLocked()) {
        maskForPrivacy('工作階段已鎖定；研究內容已遮蔽。');
        return false;
      }
      return !state.terminalLocked;
    }

    function setBusy(busy, text) {
      refs.viewButton.disabled = busy || state.terminalLocked;
      refs.refreshButton.disabled = busy || state.terminalLocked || !state.explicitSelection;
      refs.panel.setAttribute('aria-busy', String(busy));
      if (text) replaceText(refs.status, text);
    }

    function sameOriginFetch(path, init) {
      const remote = W.JEVRemote;
      if (remote && typeof remote.fetch === 'function') return remote.fetch(path, init);
      return W.fetch(path, init);
    }

    function scheduleNext() {
      clearSchedule();
      if (!checkPrivacy() || automationPaused() || state.inFlight) return;
      const now = Date.now();
      const due = [];
      if (state.nextPendingAt > 0 && state.pendingPolls < MAX_PENDING_POLLS) due.push(state.nextPendingAt);
      if (state.explicitSelection && state.selectedSymbol && state.nextAutoAt > 0) {
        due.push(Math.max(state.nextAutoAt, state.failureCooldownUntil));
      }
      if (!due.length) return;
      const delay = Math.max(0, Math.min(...due) - now);
      state.timer = W.setTimeout(onScheduled, delay);
    }

    async function readJsonResponse(response) {
      if (!response || typeof response !== 'object') throw new Error('invalid_response');
      if (typeof response.json !== 'function') {
        if (isRecord(response)) return response;
        throw new Error('invalid_response');
      }
      if (response.status === 401) {
        maskForPrivacy('工作階段已到期；研究內容已遮蔽。');
        throw new Error('session_locked');
      }
      if (response.ok !== true) throw new Error(response.status === 404 ? 'disabled' : 'request_failed');
      return response.json();
    }

    async function requestPacket(method, symbol, purpose) {
      if (state.inFlight || D.hidden || !checkPrivacy()) return false;
      clearSchedule();
      const serial = ++state.requestSerial;
      const controller = new AbortController();
      const flight = { controller, method, purpose, serial, cancelled: false };
      state.inFlight = flight;
      setBusy(true, method === 'POST' ? '正在準備指定標的的研究快照…' : '正在讀取研究快照…');
      const timeout = W.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      const init = {
        method,
        credentials: 'same-origin',
        cache: 'no-store',
        headers: method === 'POST'
          ? { Accept: 'application/json', 'Content-Type': 'application/json' }
          : { Accept: 'application/json' },
        signal: controller.signal
      };
      if (method === 'POST') init.body = JSON.stringify({ symbol });
      try {
        const response = await sameOriginFetch(method === 'POST' ? API_SELECT : API_SNAPSHOT, init);
        const raw = await readJsonResponse(response);
        if (flight.cancelled || serial !== state.requestSerial || state.terminalLocked || D.hidden) return false;
        const packet = normalizePacket(raw, pageSource(), method === 'POST' ? symbol : state.selectedSymbol || null);
        state.packet = packet;
        state.loadedOnce = true;
        renderPacket(refs, packet);
        if (packet.tainted) {
          state.nextPendingAt = 0;
          if (method === 'POST') state.failureCooldownUntil = Date.now() + FAILURE_COOLDOWN_MS;
          return false;
        }
        if (method === 'POST') {
          if (packet.symbol === symbol && !['invalid_symbol', 'forbidden'].includes(packet.reason)) {
            state.selectedSymbol = symbol;
            state.explicitSelection = true;
            refs.refreshButton.disabled = false;
            state.nextAutoAt = Date.now() + AUTO_REFRESH_MS;
            if (packet.status === 'failure' || packet.status === 'disabled') {
              state.failureCooldownUntil = Date.now() + FAILURE_COOLDOWN_MS;
            }
          } else {
            state.failureCooldownUntil = Date.now() + FAILURE_COOLDOWN_MS;
          }
        }
        if (packet.status === 'pending') {
          const index = Math.min(state.pendingPolls, PENDING_BACKOFF_MS.length - 1);
          state.nextPendingAt = Date.now() + PENDING_BACKOFF_MS[index];
        } else {
          state.nextPendingAt = 0;
          state.pendingPolls = 0;
        }
        return true;
      } catch (error) {
        if (flight.cancelled || serial !== state.requestSerial || state.terminalLocked || D.hidden) return false;
        const aborted = controller.signal.aborted;
        const reason = aborted ? 'timeout' : error && error.message === 'disabled' ? 'disabled' : 'fetch_failed';
        const packet = unknownPacket(reason, pageSource());
        state.packet = packet;
        renderPacket(refs, packet);
        if (method === 'POST') {
          state.failureCooldownUntil = Date.now() + FAILURE_COOLDOWN_MS;
          state.nextAutoAt = Math.max(state.nextAutoAt, state.failureCooldownUntil);
        } else if (purpose === 'pending' && state.pendingPolls < MAX_PENDING_POLLS) {
          const index = Math.min(state.pendingPolls, PENDING_BACKOFF_MS.length - 1);
          state.nextPendingAt = Date.now() + PENDING_BACKOFF_MS[index];
        }
        return false;
      } finally {
        W.clearTimeout(timeout);
        if (state.inFlight === flight) {
          state.inFlight = null;
          if (!state.terminalLocked) setBusy(false);
          scheduleNext();
        }
      }
    }

    function onScheduled() {
      state.timer = 0;
      if (!checkPrivacy() || automationPaused() || state.inFlight) return;
      const now = Date.now();
      if (state.nextPendingAt > 0 && now >= state.nextPendingAt && state.pendingPolls < MAX_PENDING_POLLS) {
        state.nextPendingAt = 0;
        state.pendingPolls += 1;
        requestPacket('GET', null, 'pending');
        return;
      }
      if (state.explicitSelection && state.selectedSymbol && now >= Math.max(state.nextAutoAt, state.failureCooldownUntil)) {
        requestPacket('POST', state.selectedSymbol, 'auto');
        return;
      }
      scheduleNext();
    }

    function chooseSymbol() {
      if (D.hidden || !checkPrivacy() || state.inFlight) return;
      const symbol = validateSymbol(refs.input.value, pageSource());
      if (!symbol) {
        replaceText(refs.status, pageSource() === 'DEMO'
          ? '請選擇 DEMO1USDT 至 DEMO12USDT。' : '請輸入有效的公開 USDT 標的，例如 BTCUSDT。');
        refs.input.focus();
        return;
      }
      if (Date.now() < state.failureCooldownUntil) {
        replaceText(refs.status, '上次研究請求失敗；冷卻 60 秒後可再試。');
        return;
      }
      refs.input.value = symbol;
      state.pendingPolls = 0;
      state.nextPendingAt = 0;
      requestPacket('POST', symbol, 'select');
    }

    refs.viewButton.addEventListener('click', chooseSymbol);
    refs.refreshButton.addEventListener('click', () => {
      if (!state.explicitSelection || !state.selectedSymbol || D.hidden || !checkPrivacy()) return;
      if (Date.now() < state.failureCooldownUntil) {
        replaceText(refs.status, '更新仍在 60 秒失敗冷卻中。');
        return;
      }
      requestPacket('POST', state.selectedSymbol, 'manual_refresh');
    });
    refs.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        chooseSymbol();
      }
    });
    refs.panel.addEventListener('toggle', () => {
      if (!refs.panel.open) {
        clearSchedule();
        abortInFlight();
        return;
      }
      if (!checkPrivacy()) return;
      if (!state.loadedOnce && !state.inFlight) requestPacket('GET', null, 'open');
      else scheduleNext();
    });
    D.addEventListener('visibilitychange', () => {
      if (D.hidden) {
        clearSchedule();
        abortInFlight();
      } else if (checkPrivacy()) {
        scheduleNext();
      }
    });
    W.addEventListener('pagehide', () => maskForPrivacy('頁面已離開；研究內容已遮蔽。'));
    W.addEventListener('jev:session-lock', () => maskForPrivacy('工作階段已鎖定；研究內容已遮蔽。'));
    W.addEventListener('jev:session-expired', () => maskForPrivacy('工作階段已到期；研究內容已遮蔽。'));
    W.addEventListener('jev:pause', () => { state.eventPaused = true; clearSchedule(); abortInFlight(); });
    W.addEventListener('jev:resume', () => { state.eventPaused = false; scheduleNext(); });
    if (reducedMotion && typeof reducedMotion.addEventListener === 'function') {
      reducedMotion.addEventListener('change', () => {
        if (reducedMotion.matches) { clearSchedule(); abortInFlight(); } else scheduleNext();
      });
    }
    D.addEventListener('click', (event) => {
      const target = event.target;
      if (target && target.dataset && target.dataset.panel === 'timeframeResearch') refs.panel.open = true;
    }, true);

    renderPacket(refs, unknownPacket('missing_source', pageSource()));
    if (W.JEVCompanion && typeof W.JEVCompanion.registerPanel === 'function') {
      W.JEVCompanion.registerPanel('timeframeResearch', '週期', refs.panel);
    }
    W.JEVTimeframeResearch._controller = Object.freeze({
      selectedSymbol: () => state.selectedSymbol,
      isInFlight: () => !!state.inFlight,
      timerCount: () => state.timer ? 1 : 0,
      maskForPrivacy
    });
  }

  W.JEVTimeframeResearch = {
    version: 1,
    _test: Object.freeze({
      CANONICAL,
      AUTO_REFRESH_MS,
      FAILURE_COOLDOWN_MS,
      REQUEST_TIMEOUT_MS,
      MAX_PENDING_POLLS,
      validateSymbol,
      normalizePacket,
      normalizeFrame,
      normalizeConnector,
      staleFromAsOf,
      formatNumber,
      formatUtc,
      unknownPacket
    })
  };

  function boot() {
    const run = () => D.readyState === 'loading'
      ? D.addEventListener('DOMContentLoaded', start, { once: true }) : start();
    if (W.JEVRemote && W.JEVRemote.ready) {
      Promise.resolve(W.JEVRemote.ready).then((ok) => { if (ok !== false) run(); }).catch(() => {});
    } else {
      run();
    }
  }

  boot();
})();
