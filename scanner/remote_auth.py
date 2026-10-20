# -*- coding: utf-8 -*-
"""遠端密碼登入核心：帳號存放區、工作階段、登入前節流。

安全界線：
- 帳號存放區永遠在版本庫之外，而且只存雜湊：絕不存明文密碼，也沒有任何
  預設帳號或預設密碼。原始碼樹永遠不會含真實存放區。
- 密碼雜湊固定走 hashlib.scrypt（OWASP 參數 n=2**17, r=8, p=1, dklen=32，
  maxmem 256 MiB），比較一律用 hmac.compare_digest。
  來源：https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- 帳號錯與密碼錯回同一個通用失敗，而且走同一條昂貴 KDF 路徑；同一時間
  只允許一個 KDF 運算，失敗計數（全域／來源 IP／帳號）在 KDF 之前先檢查。
- 工作階段只存「不可逆的 token 雜湊」加伺服器端中介資料；閒置 1800 秒、
  絕對上限 28800 秒（30 分鐘／8 小時），支援多裝置與伺服器端撤銷。
- 這個模組不讀環境變數、不連線、不開子行程，也不記錄帳號／密碼／雜湊／
  cookie／CSRF／原始例外。
"""
import base64
import collections
import hashlib
import hmac
import json
import os
import secrets
import stat
import string
import threading
import time

# ---- OWASP scrypt 參數（不可降低；存放區若記載更低成本一律拒絕載入） ----
SCRYPT_NAME = "scrypt"
SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 256 * 1024 * 1024

SALT_MIN_BYTES = 16
STORE_VERSION = 1
STORE_MAX_BYTES = 8192
STORE_KEYS = ("version", "kdf", "username_hash", "salt", "password_hash", "created_at", "note")
KDF_KEYS = ("name", "n", "r", "p", "dklen", "maxmem")

# ---- 密碼政策：8..128 字元（是「字元」，不是分鐘），最多 512 UTF-8 bytes ----
PASSWORD_MIN_CHARS = 8
PASSWORD_MAX_CHARS = 128
PASSWORD_MAX_BYTES = 512

# ---- 帳號正規化：ASCII 3..64 的 [a-z0-9_.@-]，去頭尾空白並小寫，只存雜湊 ----
USERNAME_MIN_CHARS = 3
USERNAME_MAX_CHARS = 64
USERNAME_ALLOWED = frozenset(string.ascii_lowercase + string.digits + "_.@-")

# ---- 工作階段 ----
SESSION_TOKEN_BYTES = 32            # 256 bit
IDLE_SECONDS = 1800                 # 閒置 30 分鐘
ABSOLUTE_SECONDS = 28800            # 絕對 8 小時
MAX_SESSIONS = 64                   # 有界：支援多裝置但不會無限成長
PREAUTH_TTL_SECONDS = 300           # 登入用一次性 preauth 憑證 5 分鐘
MAX_PREAUTH = 256

# ---- 登入前節流（有界桶） ----
FAILURE_WINDOW_SECONDS = 900.0
FAILURE_GLOBAL_LIMIT = 60
FAILURE_SCOPE_LIMIT = 10
FAILURE_MAX_SCOPES = 512


class StoreError(Exception):
    """帳號存放區錯誤；只帶固定代碼，不含路徑、密碼或例外原文。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ 文字安全

def _utf8_or_none(text):
    """把 str 編成 UTF-8；畸形 Unicode（落單代理對）回 None，不丟例外。

    JSON 可以送出 "\\ud800" 這種落單代理字元，Python 收得下但編不出去。
    任何會丟 UnicodeEncodeError 的輸入都要在這裡變成「拒絕」訊號。
    """
    if not isinstance(text, str):
        return None
    try:
        return text.encode("utf-8")
    except (UnicodeError, ValueError):
        return None


def _comparable_bytes(value):
    """回可以餵給 compare_digest 的 bytes；非 str/bytes 或畸形 Unicode 回 None。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return _utf8_or_none(value)


def _text_equal(expected, presented):
    """固定時間比較兩個秘密字串；任何不可比較的輸入一律 False，絕不丟例外。

    hmac.compare_digest 對含非 ASCII 字元的 str 會丟 TypeError，所以一律先
    轉成已驗證過的 bytes 再比。
    """
    def secret_bytes(value):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if not isinstance(value, str):
            return None
        try:
            # 本模組簽發的 session／preauth／CSRF 全是 URL-safe ASCII。
            return value.encode("ascii")
        except (UnicodeError, ValueError):
            return None

    left = secret_bytes(expected)
    right = secret_bytes(presented)
    if not left or not right:
        return False
    return hmac.compare_digest(left, right)


# ------------------------------------------------------------------ 帳號／密碼

def canonical_username(raw):
    """回正規化帳號或 None。只做去頭尾空白＋小寫，其餘字元一律拒絕。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not (USERNAME_MIN_CHARS <= len(text) <= USERNAME_MAX_CHARS):
        return None
    for ch in text:
        if ch not in USERNAME_ALLOWED:
            return None
    return text


def username_digest(canonical):
    """SHA-256(正規化帳號) 十六進位；存放區只放這個，不放帳號本身。"""
    raw = _utf8_or_none(canonical)
    if raw is None:
        raise StoreError("username_invalid")
    return hashlib.sha256(raw).hexdigest()


def password_policy_error(password):
    """回固定錯誤代碼或 None。空白與 Unicode 都允許，永不裁切或正規化。

    畸形 Unicode（落單代理對）編不成 UTF-8，歸在 password_bytes 這個既有
    固定代碼：政策檢查永遠只回代碼或 None，不會丟例外。
    """
    if not isinstance(password, str):
        return "password_type"
    if "\x00" in password:                      # NUL 會讓下游字串被截斷，保守拒絕
        return "password_nul"
    if not (PASSWORD_MIN_CHARS <= len(password) <= PASSWORD_MAX_CHARS):
        return "password_length"
    raw = _utf8_or_none(password)
    if raw is None or len(raw) > PASSWORD_MAX_BYTES:
        return "password_bytes"
    return None


def default_kdf_params():
    return {"name": SCRYPT_NAME, "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "dklen": SCRYPT_DKLEN, "maxmem": SCRYPT_MAXMEM}


def kdf_params_error(params):
    """拒絕未知 KDF、非整數參數，以及任何低於 OWASP 下限的成本。"""
    if not isinstance(params, dict) or params.get("name") != SCRYPT_NAME:
        return "kdf_unknown"
    if any(key not in KDF_KEYS for key in params):
        return "kdf_params"
    for key in ("n", "r", "p", "dklen", "maxmem"):
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return "kdf_params"
    if params["n"] < SCRYPT_N or params["r"] != SCRYPT_R or params["p"] != SCRYPT_P:
        return "kdf_cost_downgraded"
    if params["dklen"] != SCRYPT_DKLEN:
        return "kdf_params"
    if params["maxmem"] > SCRYPT_MAXMEM or params["maxmem"] < 128 * params["n"] * params["r"]:
        return "kdf_params"
    return None


def scrypt_hash(password, salt, params):
    """唯一的正式 KDF 路徑；沒有任何「降低成本」旗標。"""
    raw = _comparable_bytes(password)
    if raw is None:
        raise StoreError("password_bytes")
    return hashlib.scrypt(raw, salt=salt, n=params["n"],
                          r=params["r"], p=params["p"], dklen=params["dklen"],
                          maxmem=params["maxmem"])


# ------------------------------------------------------------------ 路徑安全

def repo_root():
    return os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def resolved_store_path(path, root=None):
    """驗證帳號存放區路徑：絕對路徑、父目錄存在、不在版本庫內、非符號連結。

    不存在的檔案也會被檢查（建立前就要擋掉不安全路徑）。
    """
    if not isinstance(path, str) or not path.strip():
        raise StoreError("auth_store_required")
    raw = os.path.abspath(path.strip())
    if not os.path.isabs(path.strip()):
        raise StoreError("auth_store_not_absolute")
    parent, name = os.path.dirname(raw), os.path.basename(raw)
    if not name or name in (".", ".."):
        raise StoreError("auth_store_bad_name")
    # 允許 create() 原子建立「最後一層」專用目錄；更上層必須已存在。
    # 這讓 Windows 能在任何密碼資料寫入前先封住新目錄的繼承 DACL。
    if os.path.lexists(parent):
        try:
            parent_info = os.lstat(parent)
        except OSError:
            raise StoreError("auth_store_parent_unreadable")
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise StoreError("auth_store_parent_reparse")
        if getattr(parent_info, "st_file_attributes", 0) & 0x400:
            raise StoreError("auth_store_parent_reparse")
        real_parent = os.path.realpath(parent)
        if os.path.normcase(real_parent) != os.path.normcase(parent):
            raise StoreError("auth_store_parent_reparse")
    else:
        grandparent = os.path.dirname(parent)
        if not os.path.isdir(grandparent) or os.path.islink(grandparent):
            raise StoreError("auth_store_parent_missing")
        real_grandparent = os.path.realpath(grandparent)
        if os.path.normcase(real_grandparent) != os.path.normcase(grandparent):
            raise StoreError("auth_store_parent_reparse")
        real_parent = os.path.join(real_grandparent, os.path.basename(parent))
    target = os.path.join(real_parent, name)
    base = os.path.normcase(repo_root() if root is None else os.path.realpath(root))
    here = os.path.normcase(real_parent)
    if here == base or here.startswith(base + os.sep):
        raise StoreError("auth_store_inside_repo")
    if os.path.islink(target):
        raise StoreError("auth_store_symlink")
    if os.path.exists(target):
        if os.path.normcase(os.path.realpath(target)) != os.path.normcase(target):
            raise StoreError("auth_store_reparse")
        if not stat.S_ISREG(os.lstat(target).st_mode):
            raise StoreError("auth_store_not_file")
    return target


def _harden(path):
    """把檔案權限收到「只有擁有者」，並讀回確認。"""
    if os.name == "nt":
        acl = _windows_acl()
        return acl is not None and acl.harden(path)
    try:
        os.chmod(path, 0o600)
        info = os.lstat(path)
        return (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and not (info.st_mode & 0o077) and _owned_by_current_user(info))
    except (OSError, AttributeError):
        return False


def _windows_acl():
    """延遲載入，讓非 Windows 平台完全不碰 Windows API。"""
    try:
        from . import remote_acl
    except (ImportError, ValueError):
        try:
            from scanner import remote_acl
        except (ImportError, ValueError):
            return None
    try:
        return remote_acl if remote_acl.available() else None
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _owned_by_current_user(info):
    """POSIX 擁有者檢查；沒有 uid API 的平台交由 DACL 路徑處理。"""
    if os.name == "nt":
        return True
    getter = getattr(os, "geteuid", None)
    return getter is not None and getattr(info, "st_uid", -1) == getter()


def _private_parent(parent):
    """既有父目錄只檢查、不修改；不安全就 fail closed。"""
    try:
        info = os.lstat(parent)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    if os.path.normcase(os.path.realpath(parent)) != os.path.normcase(parent):
        return False
    if os.name == "nt":
        acl = _windows_acl()
        return acl is not None and acl.is_private(parent)
    return not (info.st_mode & 0o077) and _owned_by_current_user(info)


def _prepare_private_parent(parent):
    """建立並封住新的專用父目錄；絕不改動既有的不安全目錄。"""
    created = False
    try:
        os.mkdir(parent, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError:
        raise StoreError("auth_store_parent_unwritable")
    if created:
        if os.name == "nt":
            acl = _windows_acl()
            safe = acl is not None and acl.harden(parent, directory=True)
        else:
            try:
                os.chmod(parent, 0o700)
            except OSError:
                safe = False
            else:
                safe = _private_parent(parent)
        if not safe:
            # 空目錄保留給操作員依原路徑回收；其中尚未寫入任何驗證資料。
            raise StoreError("auth_store_parent_permissions")
    elif not _private_parent(parent):
        raise StoreError("auth_store_parent_permissions")
    if not _private_parent(parent):
        raise StoreError("auth_store_parent_permissions")


# ------------------------------------------------------------------ 存放區

class PasswordStore:
    """單一擁有者的獨立帳號存放區（JSON，只含雜湊與成本參數）。

    kdf 參數是測試接縫：注入後只換「算雜湊的函式」，寫進檔案的成本參數
    仍然是 OWASP 正式值，所以不會有任何降低成本的旗標進到產品路徑。
    """

    _DUMMY_SALT = bytes(SALT_MIN_BYTES)
    _DUMMY_PASSWORD = "jev-remote-constant-time-filler"

    def __init__(self, path, kdf=None, kdf_timeout=30.0):
        self.path = resolved_store_path(path)
        self._kdf = kdf or scrypt_hash
        self._gate = threading.BoundedSemaphore(1)     # 同一時間只跑一個 KDF
        self._kdf_timeout = float(kdf_timeout)

    # ---------- 查詢 ----------
    def exists(self):
        try:
            return stat.S_ISREG(os.lstat(self.path).st_mode)
        except OSError:
            return False

    def read(self):
        """讀取並驗證存放區；任何不合格都丟 StoreError（固定代碼）。"""
        if not _private_parent(os.path.dirname(self.path)):
            raise StoreError("auth_store_parent_permissions")
        try:
            info = os.lstat(self.path)
        except OSError:
            raise StoreError("auth_store_missing")
        if stat.S_ISLNK(info.st_mode) or os.path.islink(self.path):
            raise StoreError("auth_store_symlink")
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise StoreError("auth_store_reparse")
        if not stat.S_ISREG(info.st_mode):
            raise StoreError("auth_store_not_file")
        if os.name == "nt":
            acl = _windows_acl()
            if acl is None or not acl.is_private(self.path):
                raise StoreError("auth_store_permissive")
        elif (info.st_mode & 0o077) or not _owned_by_current_user(info):
            raise StoreError("auth_store_permissive")
        if info.st_size > STORE_MAX_BYTES:
            raise StoreError("auth_store_too_large")
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(STORE_MAX_BYTES + 1)
        except OSError:
            raise StoreError("auth_store_unreadable")
        if len(raw) > STORE_MAX_BYTES:
            raise StoreError("auth_store_too_large")
        return _validate_record(raw)

    # ---------- 建立 ----------
    def create(self, username, password):
        """原子獨佔建立。已存在／競態／不安全路徑都失敗，而且絕不覆寫。"""
        name = canonical_username(username)
        if name is None:
            raise StoreError("username_invalid")
        bad = password_policy_error(password)
        if bad:
            raise StoreError(bad)
        params = default_kdf_params()
        salt = secrets.token_bytes(SALT_MIN_BYTES)
        digest = self.run_kdf(password, salt, params)
        record = {
            "version": STORE_VERSION,
            "kdf": params,
            "username_hash": username_digest(name),
            "salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": base64.b64encode(digest).decode("ascii"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "hash-only owner record; no plaintext password is ever stored",
        }
        body = json.dumps(record, ensure_ascii=True, sort_keys=True).encode("utf-8")
        parent = os.path.dirname(self.path)
        _prepare_private_parent(parent)
        # 父目錄建立與驗證之間若被換成連結／reparse point，重新解析會拒絕。
        if resolved_store_path(self.path) != self.path or not _private_parent(parent):
            raise StoreError("auth_store_parent_permissions")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            handle = os.open(self.path, flags, 0o600)
        except FileExistsError:
            raise StoreError("auth_store_exists")
        except OSError:
            raise StoreError("auth_store_unwritable")
        write_failed = False
        try:
            offset = 0
            while offset < len(body):
                written = os.write(handle, body[offset:])
                if (isinstance(written, bool) or not isinstance(written, int)
                        or written <= 0 or written > len(body) - offset):
                    write_failed = True
                    break
                offset += written
            if not write_failed:
                os.fsync(handle)
        except (OSError, ValueError, TypeError):
            write_failed = True
        finally:
            try:
                os.close(handle)
            except OSError:
                write_failed = True
        # 失敗的自有新檔也先收緊後原地保留，絕不刪除或碰既有 owner store。
        hardened = _harden(self.path)
        if write_failed:
            raise StoreError("auth_store_unwritable")
        if not hardened:
            raise StoreError("auth_store_permissions")
        try:
            verified = self.read()
        except StoreError:
            raise StoreError("auth_store_verify_failed")
        if verified != record:
            raise StoreError("auth_store_verify_failed")
        return dict(verified)

    # ---------- 驗證 ----------
    def run_kdf(self, password, salt, params):
        bad = kdf_params_error(params)
        if bad:
            raise StoreError(bad)
        if not self._gate.acquire(timeout=self._kdf_timeout):
            raise StoreError("kdf_busy")
        try:
            return self._kdf(password, salt, params)
        except (ValueError, MemoryError, OverflowError):
            raise StoreError("kdf_failed")
        finally:
            self._gate.release()

    def verify(self, username, password):
        """回 True/False。帳號錯、密碼錯、存放區缺失都走同一條昂貴 KDF 路徑。

        成功的必要條件同時包含「原始帳號合法」、「原始密碼通過政策」、
        「存放區存在」與「雜湊相符」：填充密碼只用來維持 KDF 時間，絕不
        能讓政策不合格的輸入（例如空字串）靠填充值本身比對成功。
        """
        try:
            record = self.read()
        except StoreError:
            record = None
        name = canonical_username(username)
        name_valid = name is not None
        policy_ok = password_policy_error(password) is None
        if record is None:
            params, salt = default_kdf_params(), self._DUMMY_SALT
            expected, name_ok = bytes(SCRYPT_DKLEN), False
        else:
            params = record["kdf"]
            salt = base64.b64decode(record["salt"])
            expected = base64.b64decode(record["password_hash"])
            name_ok = name_valid and hmac.compare_digest(
                username_digest(name), record["username_hash"])
        # 政策不合格一樣要付完整 KDF 成本（時間一致），但結果一律不採用。
        candidate = password if policy_ok else self._DUMMY_PASSWORD
        try:
            digest = self.run_kdf(candidate, salt, params)
        except StoreError:
            return False
        hash_ok = hmac.compare_digest(digest, expected)
        # 用位元 & 而非 and：不短路，避免成敗分支造成時間差。
        return bool(bool(record is not None) & bool(name_valid) & bool(name_ok)
                    & bool(policy_ok) & bool(hash_ok))


def _validate_record(raw):
    """存放區結構驗證（測試文件化的 schema）。"""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StoreError("auth_store_corrupt")
    if not isinstance(record, dict):
        raise StoreError("auth_store_corrupt")
    if any(key not in STORE_KEYS for key in record):
        raise StoreError("auth_store_unknown_field")
    if record.get("version") != STORE_VERSION:
        raise StoreError("auth_store_version")
    bad = kdf_params_error(record.get("kdf"))
    if bad:
        raise StoreError(bad)
    name_hash = record.get("username_hash")
    if not isinstance(name_hash, str) or len(name_hash) != 64 or any(
            ch not in "0123456789abcdef" for ch in name_hash):
        raise StoreError("auth_store_username_hash")
    salt = _b64(record.get("salt"))
    if len(salt) < SALT_MIN_BYTES:
        raise StoreError("auth_store_salt")
    if len(_b64(record.get("password_hash"))) != SCRYPT_DKLEN:
        raise StoreError("auth_store_password_hash")
    for key in ("created_at", "note"):
        if key in record and not isinstance(record[key], str):
            raise StoreError("auth_store_corrupt")
    return record


def _b64(value):
    if not isinstance(value, str):
        raise StoreError("auth_store_corrupt")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise StoreError("auth_store_corrupt")


# ------------------------------------------------------------------ 失敗節流

class FailureBuckets:
    """有界的固定視窗失敗計數：全域 + 每個 scope（來源 IP／帳號雜湊）。

    這些檢查一律在昂貴的 KDF 之前執行，而且字典有上限，不會無限成長。
    """

    def __init__(self, clock=time.monotonic, window_seconds=FAILURE_WINDOW_SECONDS,
                 global_limit=FAILURE_GLOBAL_LIMIT, scope_limit=FAILURE_SCOPE_LIMIT,
                 max_scopes=FAILURE_MAX_SCOPES):
        self._clock = clock
        self._window = float(window_seconds)
        self._global_limit = int(global_limit)
        self._scope_limit = int(scope_limit)
        self._max_scopes = int(max_scopes)
        self._lock = threading.Lock()
        self._scopes = collections.OrderedDict()
        self._global = [0, clock()]

    def _window_locked(self, cell):
        now = self._clock()
        if now - cell[1] >= self._window:
            cell[0], cell[1] = 0, now
        return cell

    def blocked(self, scopes):
        with self._lock:
            if self._window_locked(self._global)[0] >= self._global_limit:
                return True
            for scope in scopes:
                cell = self._scopes.get(scope)
                if cell is not None and self._window_locked(cell)[0] >= self._scope_limit:
                    return True
            return False

    def record_failure(self, scopes):
        with self._lock:
            self._window_locked(self._global)[0] += 1
            for scope in scopes:
                cell = self._scopes.get(scope)
                if cell is None:
                    cell = [0, self._clock()]
                    self._scopes[scope] = cell
                self._window_locked(cell)[0] += 1
                self._scopes.move_to_end(scope)
            while len(self._scopes) > self._max_scopes:
                self._scopes.popitem(last=False)

    def clear(self, scopes):
        with self._lock:
            for scope in scopes:
                self._scopes.pop(scope, None)

    def tracked(self):
        with self._lock:
            return len(self._scopes)


# ------------------------------------------------------------------ 工作階段

def _token():
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_key(token):
    """只有雜湊會被存起來；原始 token 只存在於 cookie 與當次請求中。

    畸形 Unicode 也要能算出確定的雜湊而不丟例外；本模組發出的 token 一律是
    URL-safe base64（ASCII），所以畸形輸入的雜湊永遠不可能命中任何紀錄。
    """
    raw = _utf8_or_none(token)
    if raw is None:
        # 查表入口會先用 _usable_token 拒絕；這裡仍維持公開 helper 永不丟例外。
        raw = b"\x00invalid-token"
    return hashlib.sha256(raw).hexdigest()


def _usable_token(token):
    """回可查表的 token 字串，否則 None（非 str／空／畸形 Unicode）。"""
    if not isinstance(token, str) or not token:
        return None
    if _utf8_or_none(token) is None:
        return None
    return token


class SessionStore:
    """工作階段存放區：只放 token 雜湊與伺服器端中介資料，單調時鐘可注入。

    CSRF token 是「刻意要讓已登入頁面讀到」的另一個秘密，所以以伺服器端
    中介資料形式保存；真正的 bearer 憑證（session token）永遠只存雜湊。
    """

    def __init__(self, clock=time.monotonic, idle_seconds=IDLE_SECONDS,
                 absolute_seconds=ABSOLUTE_SECONDS, max_sessions=MAX_SESSIONS):
        self._clock = clock
        self._idle = float(idle_seconds)
        self._absolute = float(absolute_seconds)
        self._max = int(max_sessions)
        self._lock = threading.Lock()
        self._sessions = collections.OrderedDict()

    def _purge_locked(self):
        now = self._clock()
        for key in [k for k, rec in self._sessions.items()
                    if now - rec["created"] >= self._absolute
                    or now - rec["last_seen"] >= self._idle]:
            self._sessions.pop(key, None)

    def create(self):
        """回 (session_token, csrf_token)；每次登入都是全新 token（防固定）。"""
        token, csrf = _token(), _token()
        now = self._clock()
        with self._lock:
            self._purge_locked()
            while len(self._sessions) >= self._max:
                self._sessions.popitem(last=False)
            self._sessions[token_key(token)] = {
                "created": now, "last_seen": now, "csrf": csrf,
                "id": secrets.token_hex(8),
            }
        return token, csrf

    def validate(self, token):
        """回中介資料副本或 None；成功時順延閒置時限，絕對時限不可延長。"""
        token = _usable_token(token)
        if token is None:
            return None
        key = token_key(token)
        with self._lock:
            self._purge_locked()
            record = self._sessions.get(key)
            if record is None:
                return None
            now = self._clock()
            if now - record["created"] >= self._absolute or now - record["last_seen"] >= self._idle:
                self._sessions.pop(key, None)
                return None
            record["last_seen"] = now
            self._sessions.move_to_end(key)
            return dict(record)

    def csrf_ok(self, record, presented):
        """非 ASCII 或畸形 Unicode 的 CSRF 一律 False，不會丟 TypeError。"""
        if not isinstance(record, dict):
            return False
        return _text_equal(record.get("csrf", ""), presented)

    def revoke(self, token):
        if _usable_token(token) is None:
            return False
        with self._lock:
            return self._sessions.pop(token_key(token), None) is not None

    def revoke_all(self):
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def active(self):
        with self._lock:
            self._purge_locked()
            return len(self._sessions)


class PreauthStore:
    """登入表單用的一次性 preauth 憑證（cookie + CSRF），TTL 300 秒、有界。"""

    def __init__(self, clock=time.monotonic, ttl_seconds=PREAUTH_TTL_SECONDS,
                 max_entries=MAX_PREAUTH):
        self._clock = clock
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._entries = collections.OrderedDict()

    def _purge_locked(self):
        now = self._clock()
        for key in [k for k, born in self._entries.items() if now - born[0] >= self._ttl]:
            self._entries.pop(key, None)

    def issue(self):
        token, csrf = _token(), _token()
        with self._lock:
            self._purge_locked()
            while len(self._entries) >= self._max:
                self._entries.popitem(last=False)
            self._entries[token_key(token)] = (self._clock(), csrf)
        return token, csrf

    def consume(self, token, csrf):
        """一次性：只要 token 命中就移除，之後重用一律失敗（fail-closed）。

        CSRF 比對走已驗證的 bytes：非 ASCII／畸形 Unicode 回 False，不丟例外，
        而且仍然算一次使用（token 已經被移除）。
        """
        if _usable_token(token) is None:
            return False
        with self._lock:
            self._purge_locked()
            entry = self._entries.pop(token_key(token), None)
        if entry is None:
            return False
        return _text_equal(entry[1], csrf)

    def pending(self):
        with self._lock:
            self._purge_locked()
            return len(self._entries)
