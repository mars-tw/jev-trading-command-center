/* 登入表單：同源 POST /api/auth/login，帶一次性 preauth CSRF。
   - 密碼只存在於欄位與那一次 fetch 的 body 裡，絕不寫進 localStorage、
     sessionStorage、cookie、URL 或任何畫面文字。
   - 失敗訊息一律是同一句通用文字（不分帳號錯或密碼錯）。
   - 只用 textContent 寫入畫面，不碰 innerHTML。 */
(function () {
  "use strict";

  var form = document.getElementById("loginForm");
  var username = document.getElementById("username");
  var password = document.getElementById("password");
  var button = document.getElementById("submitButton");
  var message = document.getElementById("loginMessage");
  var meta = document.querySelector('meta[name="jev-login-csrf"]');
  var csrf = meta ? meta.content : "";
  var sending = false;

  var GENERIC = "登入失敗：使用者名稱或密碼不正確。";
  var THROTTLED = "嘗試次數過多，請稍後再試。";
  var STALE = "這個登入頁已過期，請重新載入後再試。";

  function say(text) {
    message.textContent = text;
  }

  function finish() {
    sending = false;
    button.disabled = false;
    password.value = "";
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending) { return; }
    if (!csrf) { say(STALE); return; }
    sending = true;
    button.disabled = true;
    say("正在驗證…");

    fetch("/api/auth/login", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      redirect: "error",
      headers: { "Content-Type": "application/json", "X-JEV-CSRF": csrf },
      body: JSON.stringify({ username: username.value, password: password.value })
    }).then(function (response) {
      return response.json().then(function (body) {
        return { status: response.status, body: body };
      }, function () {
        return { status: response.status, body: {} };
      });
    }).then(function (result) {
      if (result.status === 200 && result.body && result.body.ok === true) {
        password.value = "";
        csrf = "";
        say("登入成功，正在開啟指揮中心…");
        window.location.replace("/stage.html");
        return;
      }
      finish();
      if (result.status === 429) { say(THROTTLED); return; }
      if (result.status === 403) { say(STALE); return; }
      say(GENERIC);
    }).catch(function () {
      finish();
      say("登入失敗：連線中斷，請重新載入後再試。");
    });
  });
}());
