# -*- coding: utf-8 -*-
"""本機初始擁有者設定：在 127.0.0.1 上用瀏覽器表單建立帳號存放區。

安全界線：
- 只綁 127.0.0.1（預設 8778），不需要也不接受公開來源；Host 必須剛好是
  本機位址加實際連接埠，Origin 必須是同一個 http loopback 來源。
- 固定的根 URL（沒有 URL token），而且只有「明示 --setup-owner 且存放區
  尚不存在」時才會啟動；存放區已存在一律拒絕，絕不覆寫。
- 使用者名稱／密碼／確認密碼只在表單輸入一次，直接進 scrypt；不會寫進
  argv、模型、聊天、日誌或任何檔案，回應也不含帳密。
- 建立使用原子獨佔 open(O_CREAT|O_EXCL) 加私有權限；競態與重複建立都失敗。
- 成功回應送出後伺服器立刻停止，不會留著一個可用的建立端點。
- 不產生也不印出任何真實密碼。
"""
import hmac
import json
import secrets
import threading
import time

from . import remote_gateway as rg
from .remote_auth import PasswordStore, StoreError, PASSWORD_MIN_CHARS, PASSWORD_MAX_CHARS

DEFAULT_SETUP_PORT = 8778
MAX_SETUP_BODY_BYTES = 2048
MAX_ATTEMPTS = 10
MAX_PENDING_CSRF = 16
SETUP_CSRF_TTL_SECONDS = 300.0
SETUP_CSRF_TOKEN_CHARS = 43
SETUP_ROUTE = "/"
SETUP_POST_ROUTE = "/api/setup/owner"

PAGE_SCRIPT = """
(function(){
  "use strict";
  var form=document.getElementById("f"),msg=document.getElementById("msg");
  var count=document.getElementById("password-count");
  var retry=document.getElementById("retry");
  var csrf=document.querySelector('meta[name="jev-setup-csrf"]').content;
  var minimum=Number(form.p.getAttribute("minlength"));
  var maximum=Number(form.p.getAttribute("maxlength"));
  var sending=false;
  var serverMessages=Object.freeze({
    already_completed:"設定已經完成，這個建立頁面不能再使用。",
    too_many_attempts:"嘗試次數已達上限，請重新啟動本機設定流程。",
    password_mismatch:"兩次輸入的密碼不一致。",
    password_type:"密碼格式無法處理。",
    password_length:"密碼必須是 8 到 128 個字元。",
    password_bytes:"密碼的 UTF-8 資料過長或含有無法處理的字元。",
    username_type:"使用者名稱格式無法處理。",
    username_length:"使用者名稱必須是 3 到 64 個字元。",
    username_chars:"使用者名稱含有不允許的字元。",
    auth_store_exists:"擁有者帳號已存在，沒有覆寫任何資料。",
    auth_store_parent_permissions:"帳號存放位置的權限不安全，沒有建立帳號。",
    auth_store_unwritable:"無法安全寫入帳號存放區，沒有建立帳號。",
    auth_store_verify_failed:"帳號資料無法安全驗證，沒有完成設定。",
    csrf_required:"本頁的安全驗證已失效。",
    default_message:"無法建立帳號，沒有保留密碼。"
  });
  function codepointLength(value){
    return Array.from(value).length;
  }
  function updateCount(){
    count.textContent="目前 "+codepointLength(form.p.value)+" 個字元";
  }
  function clearPasswords(){
    form.p.value="";
    form.c.value="";
    updateCount();
  }
  function showClientError(message){
    clearPasswords();
    retry.hidden=true;
    msg.textContent=message;
    form.p.focus();
  }
  function safeServerMessage(payload){
    var code=payload&&typeof payload.reason==="string"?payload.reason:"";
    return Object.prototype.hasOwnProperty.call(serverMessages,code)
      ?serverMessages[code]:serverMessages.default_message;
  }
  function showRemoteError(message){
    sending=false;
    clearPasswords();
    retry.hidden=false;
    msg.textContent=message;
    form.p.focus();
  }
  form.p.addEventListener("input",updateCount);
  updateCount();
  form.addEventListener("submit",function(ev){
    ev.preventDefault();
    if(sending)return;
    retry.hidden=true;
    if(form.u.value.length===0){
      showClientError("請輸入使用者名稱。");
      form.u.focus();
      return;
    }
    var length=codepointLength(form.p.value);
    if(length<minimum||length>maximum){
      showClientError("密碼必須是 8 到 128 個字元；請重新輸入。");
      return;
    }
    if(form.p.value!==form.c.value){
      showClientError("兩次輸入的密碼不一致；請重新輸入。");
      return;
    }
    msg.textContent="建立中…";
    sending=true;
    fetch("/api/setup/owner",{method:"POST",cache:"no-store",credentials:"omit",
      headers:{"Content-Type":"application/json","X-JEV-CSRF":csrf},
      body:JSON.stringify({username:form.u.value,password:form.p.value,confirm:form.c.value})
    }).then(function(r){return r.json().then(function(j){return [r.status,j];});})
      .then(function(pair){
        if(pair[0]===200&&pair[1].ok){
          form.reset();
          updateCount();
          form.querySelectorAll("input,button").forEach(function(el){el.disabled=true;});
          retry.hidden=true;
          msg.textContent="帳號已建立。設定伺服器已停止，請關掉這個分頁。";
          return;
        }
        showRemoteError(safeServerMessage(pair[1]));
      }).catch(function(){
        showRemoteError("連線中斷，沒有保留密碼。");
      });
  });
})();
"""

PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="jev-setup-csrf" content="__CSRF__">
<title>JEV 遠端存取：建立擁有者帳號</title>
<style>
body{margin:0;background:#0b0f14;color:#e8eef5;font:15px/1.6 system-ui,"Noto Sans TC",sans-serif;
 display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{width:min(420px,92vw);background:#121a24;border:1px solid #243244;border-radius:12px;padding:22px}
h1{margin:0 0 6px;font-size:18px}
p.hint{margin:0 0 16px;color:#8fa3ba;font-size:13px}
label{display:block;margin:12px 0 4px;font-size:13px;color:#b6c6d8}
input{width:100%;min-height:44px;box-sizing:border-box;padding:10px;border-radius:8px;
 border:1px solid #2b3a4e;background:#0b1118;color:#e8eef5;font-size:16px}
button{margin-top:18px;width:100%;min-height:44px;padding:11px;border:0;border-radius:8px;
 background:#2d7ef7;color:#fff;font-size:16px;cursor:pointer}
button:disabled{background:#2b3a4e;cursor:default}
#msg{margin-top:14px;min-height:20px;font-size:13px;color:#ffd38a}
#password-count{margin-top:4px;color:#8fa3ba;font-size:13px}
#retry{display:inline-block;min-height:44px;line-height:44px;color:#9fc4ff}
#retry[hidden]{display:none}
</style>
</head><body>
<main class="card">
<h1>建立遠端存取的擁有者帳號</h1>
<p class="hint" id="password-help">這一頁只在本機（127.0.0.1）開放，而且只會存雜湊。
密碼可用 __MIN__ 碼以上、好記短句或密碼管理員，不必用難記亂碼；最多 __MAX__ 個字元。
字元數以 Unicode 字元計算，允許空白，不會被裁切或正規化。</p>
<form id="f" autocomplete="off" novalidate>
<label for="u">使用者名稱（3–64 個 a-z 0-9 _ . @ -）</label>
<input id="u" name="u" type="text" autocomplete="username" spellcheck="false" required>
<label for="p">密碼</label>
<input id="p" name="p" type="password" autocomplete="new-password"
 minlength="__MIN__" maxlength="__MAX__" aria-describedby="password-help password-count" required>
<div id="password-count" aria-live="polite"></div>
<label for="c">再輸入一次密碼</label>
<input id="c" name="c" type="password" autocomplete="new-password"
 minlength="__MIN__" maxlength="__MAX__" required>
<button type="submit">建立帳號</button>
</form>
<div id="msg" role="status" aria-live="polite"></div>
<a id="retry" href="/" hidden>重新載入此頁並取得新的安全驗證碼</a>
</main>
<script>__SCRIPT__</script>
</body></html>
"""


class OwnerSetup:
    """設定伺服器狀態：一次性 CSRF、嘗試次數上限、完成旗標。"""

    def __init__(self, auth_store, port=DEFAULT_SETUP_PORT, store=None, clock=None):
        self.passwords = store or PasswordStore(auth_store)
        if self.passwords.exists():
            raise rg.ConfigError("auth_store_exists")
        self.port = port
        self.bound_port = None
        self.completed = False
        self.attempts = 0
        self.httpd = None
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._csrf = []
        self.allowed_hosts = frozenset()
        self.allowed_origins = frozenset()

    def bind(self, bound_port):
        self.bound_port = bound_port
        self.allowed_hosts = frozenset(("127.0.0.1:%d" % bound_port,
                                        "localhost:%d" % bound_port))
        self.allowed_origins = frozenset(("http://127.0.0.1:%d" % bound_port,
                                          "http://localhost:%d" % bound_port))

    def csp(self, script_hashes=()):
        return rg.content_security_policy(script_hashes)

    def issue_csrf(self):
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._csrf[:] = [(item, issued) for item, issued in self._csrf
                             if 0.0 <= now - issued < SETUP_CSRF_TTL_SECONDS]
            self._csrf.append((token, now))
            del self._csrf[:-MAX_PENDING_CSRF]          # 有界
        return token

    def consume_csrf(self, presented):
        """一次性且有期限；畸形或非 ASCII 標頭只會安全地失敗。"""
        if not isinstance(presented, str) or len(presented) != SETUP_CSRF_TOKEN_CHARS:
            return False
        try:
            candidate = presented.encode("ascii")
        except UnicodeEncodeError:
            return False
        if any(byte not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
               for byte in candidate):
            return False
        now = self._clock()
        with self._lock:
            self._csrf[:] = [(item, issued) for item, issued in self._csrf
                             if 0.0 <= now - issued < SETUP_CSRF_TTL_SECONDS]
            for index, (token, _issued) in enumerate(self._csrf):
                if hmac.compare_digest(token.encode("ascii"), candidate):
                    del self._csrf[index]
                    return True
            return False

    def page(self):
        html = (PAGE_TEMPLATE
                .replace("__MIN__", str(PASSWORD_MIN_CHARS))
                .replace("__MAX__", str(PASSWORD_MAX_CHARS))
                .replace("__SCRIPT__", PAGE_SCRIPT)
                .replace("__CSRF__", self.issue_csrf()))
        return html.encode("utf-8"), rg.script_hashes(html)

    def create(self, username, password, confirm):
        """回 (ok, 錯誤代碼)。失敗一律不建立任何檔案。"""
        with self._lock:
            if self.completed:
                return False, "already_completed"
            if self.attempts >= MAX_ATTEMPTS:
                return False, "too_many_attempts"
            self.attempts += 1
        if not isinstance(password, str) or not isinstance(confirm, str):
            return False, "password_type"
        if password != confirm:
            return False, "password_mismatch"
        try:
            self.passwords.create(username, password)
        except StoreError as exc:
            return False, exc.code
        with self._lock:
            self.completed = True
        return True, None

    def stop_soon(self):
        """成功回應送出後立刻停止；不留下可用的建立端點。"""
        httpd = self.httpd
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, daemon=True).start()


class SetupHandler(rg.RemoteHandler):
    """沿用遠端閘道的協定檢查，但只允許本機 loopback 的 Host 與 Origin。"""

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.gateway.allowed_hosts

    def _origin_ok(self):
        origin = (self.headers.get("Origin") or "").strip()
        return origin in self.gateway.allowed_origins

    def _get(self, route):
        if route != SETUP_ROUTE:
            return self.deny(404, "not_found")
        if self.gateway.completed:
            return self.deny(409, "already_completed")
        body, hashes = self.gateway.page()
        self._send(body, "text/html; charset=utf-8", script_hash_set=hashes)

    def _post(self, route):
        if route != SETUP_POST_ROUTE:
            return self.deny(404, "not_found")
        if not self._origin_ok():
            return self.deny(403, "origin_required")
        if self.gateway.completed:
            return self.deny(409, "already_completed")
        if not self.gateway.consume_csrf(self.headers.get(rg.CSRF_HEADER)):
            return self.deny(403, "csrf_required")
        payload, err = self._read_json(("username", "password", "confirm"),
                                       max_bytes=MAX_SETUP_BODY_BYTES)
        if err:
            return self.deny(*err)
        ok, reason = self.gateway.create(payload.get("username"),
                                         payload.get("password"),
                                         payload.get("confirm"))
        if not ok:
            return self.deny(400, reason)
        self.json({"ok": True, "created": True,
                   "note": "owner record stored as hashes only"})
        self.gateway.stop_soon()


def build_server(auth_store, port=DEFAULT_SETUP_PORT, store=None, clock=None):
    """只綁 127.0.0.1（port=0 給測試用）。回 (httpd, setup)。"""
    setup = OwnerSetup(auth_store, port=port, store=store, clock=clock)
    handler_cls = type("BoundSetupHandler", (SetupHandler,), {"gateway": setup})
    httpd = rg.BoundedThreadingServer((rg.BIND_HOST, port), handler_cls)
    setup.bind(httpd.server_address[1])
    setup.httpd = httpd
    return httpd, setup


def serve(auth_store, port=DEFAULT_SETUP_PORT):
    httpd, setup = build_server(auth_store, port=port)
    print("本機擁有者設定：http://127.0.0.1:%d/  （只在這台機器上開放）" % setup.bound_port)
    print("在表單裡輸入使用者名稱與密碼；成功後這支設定伺服器會立刻停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已取消（沒有建立任何檔案）。")
        return 1
    finally:
        httpd.server_close()
    if setup.completed:
        print("完成：帳號存放區已建立（只含雜湊）。接著就能用密碼模式啟動遠端閘道。")
        return 0
    print("未完成：沒有建立帳號存放區。")
    return 1


def terminal_setup(auth_store):
    """可選的終端機後援：用 getpass 讀兩次密碼，不回顯、不印出密碼。"""
    import getpass
    store = PasswordStore(auth_store)
    if store.exists():
        print("拒絕：帳號存放區已存在，絕不覆寫。")
        return 2
    username = input("使用者名稱（3–64 個 a-z 0-9 _ . @ -）：").strip()
    password = getpass.getpass("密碼（至少 %d 個字元）：" % PASSWORD_MIN_CHARS)
    confirm = getpass.getpass("再輸入一次密碼：")
    if password != confirm:
        print("拒絕：兩次輸入不一致（沒有建立任何檔案）。")
        return 2
    try:
        store.create(username, password)
    except StoreError as exc:
        print("拒絕：%s（沒有建立任何檔案）。" % exc.code)
        return 2
    del password, confirm
    print("完成：帳號存放區已建立（只含雜湊）。")
    return 0


def store_schema_example():
    """測試與文件引用的 schema 範例（值都是假的佔位字串）。"""
    return json.dumps({
        "version": 1,
        "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1,
                "dklen": 32, "maxmem": 268435456},
        "username_hash": "<sha256 hex of canonical username>",
        "salt": "<base64 of >=16 random bytes>",
        "password_hash": "<base64 of 32-byte scrypt output>",
        "created_at": "<UTC timestamp>",
        "note": "hash-only owner record; no plaintext password is ever stored",
    }, indent=2, sort_keys=True)
