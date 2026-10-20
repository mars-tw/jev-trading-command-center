/* 遠端用戶端：由遠端閘道在供應 stage.html 時注入，而且一定在既有應用程式
   script 之前載入。本機 run_app.py 的 stage.html 不會引用這支，所以本機行為
   完全不變。

   職責：
   - 只包住「同源」fetch：跨來源請求原封不動放行（這裡不會去連任何私人伴跑
     程式，手機永遠不會打 127.0.0.1:8766）。
   - 先用 /api/auth/session 取得這個工作階段的 CSRF，之後所有同源 POST 都自動
     帶上 X-JEV-CSRF。
   - 任何同源回應只要是 401，立刻封鎖畫面：清掉／隱藏既有儀表板資料、停掉音效
     與動作、取消所有輪詢與計時器、停用控制按鈕，並顯示登入連結。封鎖畫面不能
     被關掉，也不會留下任何過期資料。
   - 唯讀真實模式會明確標示，並只停用「控制」按鈕；檢視與寵物選擇照常可用。
   - CSRF 只放在記憶體，絕不寫進 localStorage、sessionStorage、cookie 或 URL。
   - 所有新的資料輸出都只用 textContent。 */
(function () {
  "use strict";

  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  var nativeSetTimeout = window.setTimeout.bind(window);
  var nativeSetInterval = window.setInterval.bind(window);
  var nativeRaf = window.requestAnimationFrame
    ? window.requestAnimationFrame.bind(window) : null;

  var csrf = "";
  var readOnly = false;
  var locked = false;
  var checking = true;
  var wasPageHidden = false;
  var timeouts = [];
  var intervals = [];
  var frames = [];
  var audioContexts = [];
  var maskedNodes = [];
  var maskedState = typeof WeakMap === "function" ? new WeakMap() : null;
  var readOnlyControls = [];
  var ready;

  /* 在任何私密 DOM 出現前先由同步 CSS 遮罩；inert/aria-hidden 防止輔助技術
     或鍵盤碰到內容。成功驗證時會精確還原節點原先的狀態。 */
  function installPrivacyStyle() {
    if (document.getElementById("jevRemotePrivacyStyle")) { return; }
    var style = document.createElement("style");
    style.id = "jevRemotePrivacyStyle";
    style.textContent = "html[data-jev-privacy] body>*:not(#jevRemoteLock){" +
      "visibility:hidden!important;pointer-events:none!important}" +
      "html[data-jev-privacy] #jevRemoteLock{visibility:visible!important}";
    (document.head || document.documentElement).appendChild(style);
  }

  function maskNode(node) {
    if (!node || node.id === "jevRemoteLock" ||
        (node.closest && node.closest("#jevRemoteLock"))) { return; }
    var saved = maskedState && maskedState.get(node);
    if (!saved) {
      saved = {
        ariaPresent: !!(node.hasAttribute && node.hasAttribute("aria-hidden")),
        ariaValue: node.getAttribute ? node.getAttribute("aria-hidden") : null,
        inertPresent: !!(node.hasAttribute && node.hasAttribute("inert")),
        inertValue: !!node.inert
      };
      if (maskedState) { maskedState.set(node, saved); }
      maskedNodes.push(node);
    }
    if (node.setAttribute) {
      node.setAttribute("aria-hidden", "true");
      node.setAttribute("inert", "");
    }
    try { node.inert = true; } catch (err) {}
  }

  function maskBodyChildren() {
    if (!document.body) { return; }
    var children = document.body.children || [];
    for (var i = 0; i < children.length; i += 1) { maskNode(children[i]); }
  }

  function restoreMaskedChildren() {
    for (var i = 0; i < maskedNodes.length; i += 1) {
      var node = maskedNodes[i];
      var saved = maskedState && maskedState.get(node);
      if (!node || !saved) { continue; }
      try { node.inert = saved.inertValue; } catch (err) {}
      if (node.setAttribute && node.removeAttribute) {
        if (saved.inertPresent) { node.setAttribute("inert", ""); }
        else { node.removeAttribute("inert"); }
        if (saved.ariaPresent) { node.setAttribute("aria-hidden", saved.ariaValue); }
        else { node.removeAttribute("aria-hidden"); }
      }
      if (maskedState) { maskedState.delete(node); }
    }
    maskedNodes = [];
  }

  function beginPrivacyMask() {
    checking = true;
    installPrivacyStyle();
    if (document.documentElement) {
      document.documentElement.setAttribute("data-jev-privacy", locked ? "locked" : "checking");
    }
    maskBodyChildren();
    showLockOverlay(locked ? "locked" : "checking");
  }

  function releasePrivacyMask() {
    if (locked) { return; }
    checking = false;
    restoreMaskedChildren();
    if (document.documentElement) { document.documentElement.removeAttribute("data-jev-privacy"); }
    var overlay = document.getElementById("jevRemoteLock");
    if (overlay && overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
  }

  installPrivacyStyle();
  if (document.documentElement) {
    document.documentElement.setAttribute("data-jev-privacy", "checking");
  }
  maskBodyChildren();

  /* ---------- 計時器／動畫追蹤：封鎖時才能真的停下來 ---------- */
  window.setTimeout = function (fn, delay) {
    if (locked) { return 0; }
    var id = nativeSetTimeout.apply(null, arguments);
    timeouts.push(id);
    if (timeouts.length > 4096) { timeouts.splice(0, 2048); }
    return id;
  };
  window.setInterval = function (fn, delay) {
    if (locked) { return 0; }
    var id = nativeSetInterval.apply(null, arguments);
    intervals.push(id);
    return id;
  };
  if (nativeRaf) {
    window.requestAnimationFrame = function (fn) {
      if (locked) { return 0; }
      var id = nativeRaf(fn);
      frames.push(id);
      if (frames.length > 4096) { frames.splice(0, 2048); }
      return id;
    };
  }

  /* ---------- 音效追蹤：封鎖時要真的靜音 ---------- */
  ["AudioContext", "webkitAudioContext"].forEach(function (name) {
    var Original = window[name];
    if (typeof Original !== "function") { return; }
    function Tracked() {
      if (locked) { throw new Error("jev_session_locked"); }
      var ctx = new Original(arguments[0]);
      audioContexts.push(ctx);
      return ctx;
    }
    Tracked.prototype = Original.prototype;
    window[name] = Tracked;
  });

  /* ---------- 同源判斷 ---------- */
  function targetUrl(input) {
    try {
      var raw = typeof input === "string" ? input
        : (input && typeof input.url === "string" ? input.url : "");
      return new URL(raw, window.location.href);
    } catch (err) {
      return null;
    }
  }

  function isSameOrigin(input) {
    var url = targetUrl(input);
    return !!url && url.origin === window.location.origin;
  }

  function methodOf(input, init) {
    var method = (init && init.method) || (input && input.method) || "GET";
    return String(method).toUpperCase();
  }

  /* ---------- 封鎖（session 失效） ---------- */
  var DATA_SINKS = ["evLog", "posRows", "riskBox", "feedMeta", "tickRows", "seatRows",
    "taskFeed", "sigStats", "nowDoing", "phaseTitle", "decCard", "hEquity", "hPnl",
    "hWinPos", "hScan", "hMode", "hModeTag", "jcStatus", "jcMeta", "clsTxt",
    "chipC", "chipG", "chipR", "ndA", "ndD", "ndN", "lwStatus"];

  function stopEverything() {
    intervals.forEach(function (id) { try { window.clearInterval(id); } catch (e) {} });
    timeouts.forEach(function (id) { try { window.clearTimeout(id); } catch (e) {} });
    if (window.cancelAnimationFrame) {
      frames.forEach(function (id) { try { window.cancelAnimationFrame(id); } catch (e) {} });
    }
    intervals = []; timeouts = []; frames = [];
    audioContexts.forEach(function (ctx) {
      try { if (ctx.suspend) { ctx.suspend(); } } catch (e) {}
      try { if (ctx.close) { ctx.close(); } } catch (e) {}
    });
    audioContexts = [];
  }

  function clearDashboard() {
    DATA_SINKS.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.textContent = ""; }
    });
    if (document.body) { document.body.classList.add("noanim", "jev-locked"); }
  }

  function disableAllControls() {
    var nodes = document.querySelectorAll("button, input, select, textarea");
    for (var i = 0; i < nodes.length; i += 1) {
      if (!nodes[i].closest || !nodes[i].closest("#jevRemoteLock")) {
        nodes[i].disabled = true;
      }
    }
  }

  function overlayCopy(state) {
    if (state === "checking") {
      return ["正在驗證工作階段", "確認登入狀態後才會顯示指揮台資料。", ""];
    }
    if (state === "logout-unconfirmed") {
      return ["畫面已在本機鎖定", "伺服器尚未確認登出。畫面資料已隱藏，請重新登入或稍後再試。", "重新登入"];
    }
    if (state === "logged-out") {
      return ["已登出", "伺服器已撤銷這個工作階段，畫面資料已清除。", "重新登入"];
    }
    return ["工作階段無法驗證", "為了安全，畫面上的資料已經清除。請重新登入以繼續。", "重新登入"];
  }

  function showLockOverlay(state) {
    if (!document.body) { return; }
    var copy = overlayCopy(state);
    var box = document.getElementById("jevRemoteLock");
    if (!box) {
      box = document.createElement("div");
      box.id = "jevRemoteLock";
      box.style.cssText = "position:fixed;inset:0;z-index:2147483647;display:flex;" +
        "align-items:center;justify-content:center;background:#070b10;color:#e8eef5;" +
        "font:15px/1.6 system-ui,'Noto Sans TC',sans-serif;visibility:visible;padding:20px";
      var card = document.createElement("div");
      card.style.cssText = "max-width:420px;padding:26px;border:1px solid #243244;" +
        "border-radius:14px;background:#121a24;text-align:center";
      var title = document.createElement("h2");
      title.setAttribute("data-jev-lock-title", "");
      title.style.cssText = "margin:0 0 10px;font-size:18px";
      var text = document.createElement("p");
      text.setAttribute("data-jev-lock-text", "");
      text.style.cssText = "margin:0 0 18px;font-size:13.5px;color:#9fb3c8";
      var link = document.createElement("a");
      link.setAttribute("data-jev-lock-link", "");
      link.href = "/login";
      link.style.cssText = "display:inline-block;min-height:44px;padding:11px 20px;border-radius:9px;" +
        "background:#168ca8;color:#fff;text-decoration:none;font-size:15px";
      card.appendChild(title);
      card.appendChild(text);
      card.appendChild(link);
      box.appendChild(card);
      document.body.appendChild(box);
    }
    box.dataset.state = state;
    box.setAttribute("role", state === "checking" ? "status" : "alertdialog");
    box.setAttribute("aria-live", state === "checking" ? "polite" : "assertive");
    var titleNode = box.querySelector("[data-jev-lock-title]");
    var textNode = box.querySelector("[data-jev-lock-text]");
    var linkNode = box.querySelector("[data-jev-lock-link]");
    if (titleNode) { titleNode.textContent = copy[0]; }
    if (textNode) { textNode.textContent = copy[1]; }
    if (linkNode) { linkNode.textContent = copy[2]; linkNode.hidden = !copy[2]; }
  }

  function lockdown(state) {
    if (!locked) {
      locked = true;
      checking = false;
      csrf = "";
      stopEverything();
      clearDashboard();
    }
    installPrivacyStyle();
    if (document.documentElement) { document.documentElement.setAttribute("data-jev-privacy", "locked"); }
    maskBodyChildren();
    disableAllControls();
    showLockOverlay(state || "locked");
  }

  /* ---------- 唯讀真實模式：只停用控制，檢視與寵物選擇保留 ---------- */
  var CONTROL_IDS = ["btnPause", "lwToggle"];

  function saveReadOnlyControl(el) {
    for (var i = 0; i < readOnlyControls.length; i += 1) {
      if (readOnlyControls[i].el === el) { return; }
    }
    readOnlyControls.push({ el: el, disabled: !!el.disabled, title: el.title || "" });
  }

  function clearReadOnly() {
    for (var i = 0; i < readOnlyControls.length; i += 1) {
      var item = readOnlyControls[i];
      if (!item.el) { continue; }
      item.el.disabled = item.disabled;
      item.el.title = item.title;
    }
    readOnlyControls = [];
    var oldBanner = document.getElementById("jevRemoteMode");
    if (oldBanner && oldBanner.parentNode) { oldBanner.parentNode.removeChild(oldBanner); }
  }

  function applyReadOnly() {
    if (locked) { return; }
    if (!readOnly) { clearReadOnly(); return; }
    CONTROL_IDS.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) {
        saveReadOnlyControl(el);
        el.disabled = true;
        el.title = "唯讀遠端模式：這個交易或排程控制已停用";
      }
    });
    var demoBar = document.getElementById("demoCtl");
    if (demoBar) {
      var buttons = demoBar.querySelectorAll("button");
      for (var i = 0; i < buttons.length; i += 1) {
        saveReadOnlyControl(buttons[i]);
        buttons[i].disabled = true;
      }
    }
    var banner = document.getElementById("jevRemoteMode");
    if (!banner && document.body) {
      banner = document.createElement("div");
      banner.id = "jevRemoteMode";
      banner.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:2147483000;" +
        "padding:6px 10px;background:#2a1e07;color:#ffd38a;text-align:center;" +
        "font:12.5px/1.45 system-ui,'Noto Sans TC',sans-serif";
      banner.textContent = "遠端唯讀模式：交易與排程控制已停用，可選研究標的／檢視夥伴。";
      document.body.appendChild(banner);
    }
  }

  /* ---------- 登出 ---------- */
  function logout() {
    if (!nativeFetch || locked || !csrf) {
      lockdown("logout-unconfirmed");
      return Promise.resolve(false);
    }
    return nativeFetch("/api/auth/logout", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-JEV-CSRF": csrf },
      body: "{}"
    }).then(function (response) {
      if (!response || !response.ok) { throw new Error("logout_unconfirmed"); }
      return response.json();
    }).then(function (body) {
      if (!body || body.logged_out !== true) {
        lockdown("logout-unconfirmed");
        return false;
      }
      lockdown("logged-out");
      window.location.replace("/login");
      return true;
    }, function () {
      lockdown("logout-unconfirmed");
      return false;
    });
  }

  function wireLogout() {
    var nodes = document.querySelectorAll("#btnLogout, [data-jev-logout]");
    for (var i = 0; i < nodes.length; i += 1) {
      if (!nodes[i].dataset.jevLogoutWired) {
        nodes[i].dataset.jevLogoutWired = "1";
        nodes[i].addEventListener("click", function (event) {
          event.preventDefault();
          logout();
        });
      }
    }
  }

  /* ---------- 工作階段 bootstrap / BFCache 重新驗證 ---------- */
  function validSessionBody(body) {
    return !!body && body.ok === true && typeof body.csrf === "string" && body.csrf.length > 0;
  }

  function validateSession() {
    beginPrivacyMask();
    if (!nativeFetch) {
      lockdown("locked");
      return Promise.resolve(false);
    }
    return nativeFetch("/api/auth/session", {
      cache: "no-store",
      credentials: "same-origin"
    }).then(function (response) {
      if (!response || !response.ok) { throw new Error("session_unavailable"); }
      return response.json();
    }).then(function (body) {
      if (!validSessionBody(body)) {
        lockdown("locked");
        return false;
      }
      if (locked) { return false; }
      csrf = body.csrf;
      readOnly = body.read_only === true;
      releasePrivacyMask();
      applyReadOnly();
      wireLogout();
      return true;
    }, function () {
      lockdown("locked");
      return false;
    });
  }

  function setReady(promise) {
    ready = promise;
    if (window.JEVRemote) { window.JEVRemote.ready = promise; }
    return promise;
  }

  setReady(validateSession());

  /* ---------- fetch 包裝：只動同源請求 ---------- */
  if (nativeFetch) {
    window.fetch = function (input, init) {
      if (!isSameOrigin(input)) {
        return nativeFetch(input, init);       // 跨來源：原封不動
      }
      if (locked) {
        return Promise.reject(new Error("jev_session_locked"));
      }
      return ready.then(function () {
        if (locked || checking) { return Promise.reject(new Error("jev_session_locked")); }
        var args = methodOf(input, init) === "POST" ? withCsrf(input, init) : [input, init];
        return nativeFetch.apply(null, args).then(watch);
      });
    };
  }

  function withCsrf(input, init) {
    var options = {};
    var key;
    if (init) { for (key in init) { if (init.hasOwnProperty(key)) { options[key] = init[key]; } } }
    var headers = new Headers((init && init.headers)
      || (typeof input !== "string" && input && input.headers) || {});
    if (csrf) { headers.set("X-JEV-CSRF", csrf); }
    options.headers = headers;
    options.credentials = "same-origin";
    if (typeof input === "string") { return [input, options]; }
    return [new Request(input, options), undefined];
  }

  function watch(response) {
    if (response && response.status === 401) { lockdown(); }
    return response;
  }

  /* ---------- 既有 script 之後才出現的節點也要處理 ---------- */
  function sweep() {
    if (checking || locked) {
      maskBodyChildren();
      showLockOverlay(locked ? "locked" : "checking");
      if (locked) { disableAllControls(); }
      return;
    }
    applyReadOnly();
    wireLogout();
  }

  document.addEventListener("DOMContentLoaded", sweep);
  window.addEventListener("load", sweep);
  window.addEventListener("pagehide", function () {
    wasPageHidden = true;
    beginPrivacyMask();
  });
  window.addEventListener("pageshow", function (event) {
    if (!locked && (wasPageHidden || (event && event.persisted))) {
      wasPageHidden = false;
      setReady(validateSession());
    }
  });
  if (window.MutationObserver && document.documentElement) {
    new MutationObserver(sweep).observe(document.documentElement,
      { childList: true, subtree: true });
  }

  window.JEVRemote = {
    logout: logout,
    isLocked: function () { return locked; },
    isChecking: function () { return checking; },
    isReadOnly: function () { return readOnly; },
    hasCsrf: function () { return csrf.length > 0; },
    ready: ready
  };
}());
