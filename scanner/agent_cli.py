# -*- coding: utf-8 -*-
"""原生 CLI 子行程執行層（研究用，不接任何交易寫入 API）。

本模組只負責四個已安裝的原生 CLI 家族（CLAUDE / CODEX / GROK / AGY）：
  1. 安全探測：PATH 找執行檔 + 讀 --help 確認「安全旗標」能力（不讀任何認證檔、不做計費備援）。
  2. 真實子行程：每個工作一個 Popen、自有工作目錄、最小環境變數、硬逾時、輸出上限。
  3. 行程樹收尾：Windows Job Object kill-on-close，不可用時 taskkill /PID <自有PID> /T /F；
     Unix 新 session + 記住自己的 pgid 再 killpg（不依賴 getpgid(已死的父行程)）。
  4. 嚴格解析：先擋錯誤信封，再驗型別化 JSON；完全不做散文／大括號片段撈取。
  5. 清理：只「回收」自有暫存目錄（Windows IFileOperation + FOFX_RECYCLEONDELETE）。
     本檔案任何路徑都不呼叫 os.remove / os.rmdir / os.unlink / shutil.rmtree，
     安全回收不可用時「保留目錄 + 回一句精簡警告」，絕不退化成永久刪除，也不清空回收桶。

- QWEN is disabled by default; this adapter is not implemented.
"""
import ctypes
import json
import math
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------- 常數 / 介面

ALIAS_ORDER = ("CLAUDE", "CODEX", "GROK", "AGY")
DISABLED_ALIASES = ("QWEN",)
DISABLED_REASON = {"QWEN": "預設未啟用，未提供此適配器"}

VERDICTS = ("support", "challenge", "unclear")

DEFAULT_TIMEOUT_SECONDS = 150
DEFAULT_MAX_OUTPUT_BYTES = 128 * 1024  # stdout + stderr 合計上限
HELP_PROBE_TIMEOUT = 45

FINDING_MAX = 350
EVIDENCE_MAX_ITEMS = 4
EVIDENCE_ITEM_MAX = 160

# 失敗種類 -> 對外安全說明（固定句子，永不回填原始輸出）
FAILURE_TEXT = {
    "quota": "供應商回報額度／限流，已進入冷卻",
    "auth": "供應商回報需要重新登入，已進入冷卻",
    "invalid_json": "回覆不是合規的型別化 JSON，已丟棄",
    "provider_error": "供應商回傳錯誤信封，不計入完成",
    "nonzero_exit": "CLI 以非零結束碼結束",
    "timeout": "超過單次硬逾時，已終止自有行程樹",
    "output_overflow": "輸出超過上限，已終止自有行程樹",
    "spawn_error": "無法啟動自有子行程",
    "unsafe_environment": "無法建立受控行程樹，未執行 CLI",
    "unavailable": "找不到可用的原生 CLI 能力證據",
}

_QUOTA_MARKERS = (
    "resource_exhausted", "rate limit", "rate_limit", "ratelimit", "quota",
    "credit balance", "credits balance", "insufficient_quota", "too many requests",
    "429", "usage limit", "overloaded",
)
_AUTH_MARKERS = (
    "unauthorized", "not logged in", "please log in", "please run /login", "login required",
    "authentication", "invalid api key", "invalid_api_key", "oauth token", "401", "403",
    "permission denied by provider",
)

# ---------------------------------------------------------- 最小環境變數白名單

# 只給這些；其餘一律不繼承（含 TYPESAFE_API_KEY / BINANCE_* / BYBIT_* /
# ANTHROPIC_API_KEY / OPENAI_API_KEY 等）。CLI 自己的原生登入資料存在
# 使用者家目錄內，由 CLI 內部讀取——本程式不讀、不印、不轉傳。
_ENV_CANONICAL = (
    ("PATH", "PATH"),
    ("SYSTEMROOT", "SystemRoot"),
    ("WINDIR", "windir"),
    ("TEMP", "TEMP"),
    ("TMP", "TMP"),
    ("USERPROFILE", "USERPROFILE"),
    ("HOME", "HOME"),
    ("APPDATA", "APPDATA"),
    ("LOCALAPPDATA", "LOCALAPPDATA"),
)
# 刻意加入的非機密顯示／語系變數（讓非互動輸出乾淨、可解析）
DELIBERATE_ENV = {"NO_COLOR": "1", "TERM": "dumb", "LANG": "en_US.UTF-8"}

_FORBIDDEN_ENV_TOKENS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "SESSION",
    "TYPESAFE", "BINANCE", "BYBIT", "ANTHROPIC", "OPENAI", "XAI", "GEMINI", "GOOGLE",
    "AWS", "AZURE",
)


def _env_name_allowed(name):
    up = name.upper()
    return not any(tok in up for tok in _FORBIDDEN_ENV_TOKENS)


def build_child_env():
    """從白名單重建子行程環境；絕不複製 os.environ。"""
    env = {}
    for src, dst in _ENV_CANONICAL:
        if not _env_name_allowed(src):
            continue
        val = os.environ.get(src)
        if isinstance(val, str) and val:
            env[dst] = val
    for k, v in DELIBERATE_ENV.items():
        if _env_name_allowed(k):
            env[k] = v
    return env


# ------------------------------------------------------------ 安全字串處理

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]{1,79}$")


def clean_text(value, limit):
    """壓掉控制字元與多餘空白並截斷；用於 finding/evidence 等要外曝的欄位。"""
    if not isinstance(value, str):
        return ""
    txt = _CTRL_RE.sub(" ", value)
    txt = " ".join(txt.split())
    return txt[:limit]


# ------------------------------------------------------- 嚴格 JSON 結果解析
#
# 規則（刻意嚴格，寧可判失敗也不要假完成）：
#   * 只接受「整份完整 JSON」或「整行完整 JSON 的 JSONL 信封」。
#   * 任何已知錯誤信封在「驗證 payload 之前」就先擋掉，且不得用別的片段逃脫。
#   * 不掃散文、不掃大括號片段、不看 stderr。
#   * 巢狀字串只有在「整個字串本身就是 JSON」時才往下解析。

_RESULT_KEYS = (
    "result", "response", "output", "output_text", "text", "message", "content",
    "final", "final_message", "agent_message", "data", "payload", "value",
)
_MODEL_KEYS = ("model", "modelId", "model_id", "model_name", "modelVersion", "model_version")
_MAX_DEPTH = 5
_MAX_LIST = 8
_MAX_JSONL_LINES = 400
_MAX_NESTED_STRING = 262144

# 研究用途：帶有交易動作語意的欄位一律視為不合規（本線絕不產生下單建議）
_ACTION_KEYS = (
    "order", "orders", "trade", "trades", "side", "size", "qty", "quantity", "leverage",
    "entry", "stop_loss", "take_profit", "tp", "sl", "action", "actions",
    "recommendation", "recommendations", "position", "allocation",
)

_ERROR_FLAG_KEYS = ("is_error", "isError", "error", "errors", "error_message", "errorMessage")
_ERROR_STATE_KEYS = ("subtype", "type", "status", "state", "outcome", "result_type")
_ERROR_STATE_WORDS = (
    "error", "failed", "failure", "fatal", "denied", "refus", "abort", "cancel",
    "timeout", "timed_out", "exceeded", "unauthorized", "forbidden",
)


class ParseError(Exception):
    """解析失敗；訊息只用固定分類字串，不含供應商原始輸出。"""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def _reject_non_finite(value):
    raise ValueError("non-finite JSON constant rejected: %s" % value)


def loads_strict(text):
    """完整 JSON 解析；拒絕 NaN/Infinity，失敗回 None（不丟例外給呼叫端）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text, parse_constant=_reject_non_finite)
    except (ValueError, TypeError, RecursionError):
        return None


def _unwrap_single_fence(text):
    """只處理「整段就是一個 ``` 區塊」的情形；不是就原樣回傳（不做片段撈取）。"""
    body = text.strip()
    if not body.startswith("```") or not body.endswith("```") or body.count("```") != 2:
        return body
    nl = body.find("\n")
    if nl < 0:
        return body
    head = body[3:nl].strip().lower()
    if head and head not in ("json", "json5", "jsonc"):
        return body
    return body[nl + 1:-3].strip()


def is_error_envelope(obj):
    """已知錯誤信封判定（在任何 payload 驗證之前執行）。"""
    if not isinstance(obj, dict):
        return False
    if obj.get("is_error") is True or obj.get("isError") is True:
        return True
    for key in ("error", "errors", "error_message", "errorMessage"):
        val = obj.get(key)
        if isinstance(val, bool):
            if val:
                return True
        elif isinstance(val, (dict, list)) and val:
            return True
        elif isinstance(val, str) and val.strip():
            return True
    for key in _ERROR_STATE_KEYS:
        val = obj.get(key)
        if isinstance(val, str):
            low = val.strip().lower()
            if any(word in low for word in _ERROR_STATE_WORDS):
                return True
    code = obj.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return True
    return False


def validate_payload(obj):
    """只接受型別化研究結果；任何不合規都回 None（嚴格，不做善意補救）。"""
    if not isinstance(obj, dict):
        return None
    for key in _ACTION_KEYS:
        if key in obj:
            return None
    verdict = obj.get("verdict")
    if not isinstance(verdict, str):
        return None
    verdict = verdict.strip().lower()
    if verdict not in VERDICTS:
        return None
    finding = obj.get("finding")
    if not isinstance(finding, str):
        return None
    finding = clean_text(finding, FINDING_MAX)
    if not finding:
        return None
    raw_ev = obj.get("evidence")
    if not isinstance(raw_ev, list) or len(raw_ev) > EVIDENCE_MAX_ITEMS:
        return None
    evidence = []
    for item in raw_ev:
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if not isinstance(item, str):
            return None
        txt = clean_text(item, EVIDENCE_ITEM_MAX)
        if not txt:
            return None
        evidence.append(txt)
    return {"verdict": verdict, "finding": finding, "evidence": evidence}


class _Ctx(object):
    __slots__ = ("model",)

    def __init__(self):
        self.model = None

    def note(self, obj):
        # Keep configured labels UNVERIFIED unless trusted model provenance is implemented.
        # Assistant-produced body fields never constitute native model identity metadata.
        return None

def _preflight_envelope(root):
    """Inspect the entire bounded envelope before selecting any successful payload."""
    pending = [(root, 0)]
    nodes = 0
    forbidden = ("tool_use", "tool_call", "function_call", "command_execution",
                 "mcp_tool", "web_search", "file_change", "custom_tool")
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if nodes > 600 or depth > 10:
            raise ParseError("invalid_json")
        if isinstance(node, dict):
            if is_error_envelope(node):
                raise ParseError("provider_error")
            for key in ("type", "event", "kind", "subtype"):
                value = node.get(key)
                if isinstance(value, str) and any(word in value.lower() for word in forbidden):
                    raise ParseError("unexpected_tool")
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    pending.append((value, depth + 1))
                elif key in _RESULT_KEYS and isinstance(value, str):
                    body = _unwrap_single_fence(value)
                    if len(body) <= _MAX_NESTED_STRING and body.startswith(("{", "[")):
                        decoded = loads_strict(body)
                        if decoded is not None:
                            pending.append((decoded, depth + 1))
        elif isinstance(node, list):
            if len(node) > _MAX_LIST:
                raise ParseError("invalid_json")
            pending.extend((item, depth + 1) for item in node)


def _typed_from(node, ctx, depth=0):
    """沿著「明確白名單鍵」往下找型別化 payload；找不到就丟 ParseError。"""
    if depth > _MAX_DEPTH:
        raise ParseError("invalid_json")
    if isinstance(node, dict):
        if is_error_envelope(node):
            raise ParseError("provider_error")
        ctx.note(node)
        if "verdict" in node:
            payload = validate_payload(node)
            if payload is None:
                raise ParseError("invalid_json")
            return payload
        for key in _RESULT_KEYS:
            if key not in node:
                continue
            try:
                return _typed_from(node[key], ctx, depth + 1)
            except ParseError as exc:
                if exc.kind == "provider_error":
                    raise
        raise ParseError("invalid_json")
    if isinstance(node, list):
        if len(node) > _MAX_LIST:
            raise ParseError("invalid_json")
        for item in reversed(node):
            try:
                return _typed_from(item, ctx, depth + 1)
            except ParseError as exc:
                if exc.kind == "provider_error":
                    raise
        raise ParseError("invalid_json")
    if isinstance(node, str):
        if len(node) > _MAX_NESTED_STRING:
            raise ParseError("invalid_json")
        body = _unwrap_single_fence(node)
        obj = loads_strict(body)
        if obj is None:
            raise ParseError("invalid_json")
        return _typed_from(obj, ctx, depth + 1)
    raise ParseError("invalid_json")


def _parse_whole_json(text):
    """CLAUDE / GROK / AGY：stdout 必須是一份完整 JSON 文件。"""
    if not isinstance(text, str) or not text.strip():
        raise ParseError("invalid_json")
    root = loads_strict(_unwrap_single_fence(text))
    if root is None:
        raise ParseError("invalid_json")
    _preflight_envelope(root)
    ctx = _Ctx()
    payload = _typed_from(root, ctx)
    return payload, ctx.model


def _codex_item_text(obj):
    if obj.get("type") != "item.completed":
        return None
    item = obj.get("item")
    if isinstance(item, dict) and item.get("type") == "agent_message":
        text = item.get("text")
        return text if isinstance(text, str) else None
    return None


def _parse_codex_jsonl(text):
    if not isinstance(text, str) or not text.strip():
        raise ParseError("invalid_json")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > _MAX_JSONL_LINES:
        raise ParseError("invalid_json")
    ctx = _Ctx()
    final_text = None
    terminal_success = False
    for line in lines:
        obj = loads_strict(line)
        if not isinstance(obj, dict):
            raise ParseError("invalid_json")
        _preflight_envelope(obj)
        ctx.note(obj)
        for name in ("item", "msg", "session", "thread"):
            child = obj.get(name)
            if isinstance(child, dict):
                ctx.note(child)
        event = obj.get("type")
        if event == "turn.started":
            final_text = None
        got = _codex_item_text(obj)
        if got is not None:
            final_text = got
        terminal_success = event == "turn.completed" and final_text is not None
    if not terminal_success:
        raise ParseError("invalid_json")
    root = loads_strict(_unwrap_single_fence(final_text))
    if root is None:
        raise ParseError("invalid_json")
    _preflight_envelope(root)
    return _typed_from(root, ctx), ctx.model



_PARSERS = {
    "CLAUDE": _parse_whole_json,
    "CODEX": _parse_codex_jsonl,
    "GROK": _parse_whole_json,
    "AGY": _parse_whole_json,
}


def parse_cli_output(alias, stdout_text):
    """回 (payload, model_reported)；失敗丟 ParseError('invalid_json'|'provider_error')。"""
    parser = _PARSERS.get(alias)
    if parser is None:
        raise ParseError("invalid_json")
    return parser(stdout_text)


def classify_failure(text, exit_code, default="nonzero_exit"):
    """只用關鍵字把失敗分成 quota/auth/其他；text 只在記憶體中看過，不落地。"""
    low = (text or "").lower()
    if any(m in low for m in _QUOTA_MARKERS):
        return "quota"
    if any(m in low for m in _AUTH_MARKERS):
        return "auth"
    return default


# ------------------------------------------------- Windows 行程樹收尾（ctypes）

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000

if IS_WINDOWS:  # pragma: no cover - 平台相依
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_ulong),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_ulong),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_ulong),
                    ("SchedulingClass", ctypes.c_ulong)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _K32 = None
    _K32_LOCK = threading.Lock()

    def _k32():
        """取得已設定好 argtypes/restype 的 kernel32（64 位元 HANDLE 安全）。"""
        global _K32
        with _K32_LOCK:
            if _K32 is not None:
                return _K32
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            k.CreateJobObjectW.restype = wintypes.HANDLE
            k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  wintypes.LPVOID, wintypes.DWORD]
            k.SetInformationJobObject.restype = wintypes.BOOL
            k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k.AssignProcessToJobObject.restype = wintypes.BOOL
            k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k.TerminateJobObject.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.CloseHandle.restype = wintypes.BOOL
            k.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
            k.GetDriveTypeW.restype = wintypes.UINT
            k.GetVolumeInformationW.argtypes = [
                wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
            k.GetVolumeInformationW.restype = wintypes.BOOL
            k.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                                            wintypes.DWORD]
            k.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
            _K32 = k
            return _K32


def _win_create_kill_on_close_job():  # pragma: no cover - 平台相依
    try:
        k = _k32()
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            k.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return None


def _win_assign(handle, proc_handle):  # pragma: no cover - 平台相依
    try:
        return bool(_k32().AssignProcessToJobObject(handle, int(proc_handle)))
    except Exception:
        return False


def _win_kill_job(handle):  # pragma: no cover - 平台相依
    """回傳 API 的真實結果（不再一律 True）。"""
    if not handle:
        return False
    try:
        return bool(_k32().TerminateJobObject(handle, 1))
    except Exception:
        return False


def _win_close(handle):  # pragma: no cover - 平台相依
    if not handle:
        return False
    try:
        return bool(_k32().CloseHandle(handle))
    except Exception:
        return False


def _win_resume(proc_handle):
    """Resume only a child already contained in our kill-on-close job."""
    try:
        nt = ctypes.WinDLL("ntdll", use_last_error=True)
        nt.NtResumeProcess.argtypes = [ctypes.c_void_p]
        nt.NtResumeProcess.restype = ctypes.c_long
        return nt.NtResumeProcess(int(proc_handle)) == 0
    except Exception:
        return False


def _taskkill(pid):  # pragma: no cover - 平台相依
    """備援：只殺「自己啟動且已知 PID」的那一棵樹。"""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    cmd = [exe if os.path.exists(exe) else "taskkill", "/PID", str(int(pid)), "/T", "/F"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=build_child_env(), timeout=20, creationflags=_CREATE_NO_WINDOW)
        return p.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------- 自有子行程（真實 Popen）

class FinishedRaw(object):
    __slots__ = ("exit_code", "stdout_text", "stderr_text", "timed_out", "overflow",
                 "duration_ms", "bytes_captured")

    def __init__(self, exit_code, stdout_text, stderr_text, timed_out, overflow,
                 duration_ms, bytes_captured):
        self.exit_code = exit_code
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text
        self.timed_out = timed_out
        self.overflow = overflow
        self.duration_ms = duration_ms
        self.bytes_captured = bytes_captured


class OwnedProcess(object):
    """一個自有的原生子行程：硬逾時、輸出上限、整棵行程樹收尾。

    stdout 與 stderr 分開收（結果只解析 stdout；stderr 僅留在記憶體供失敗分類），
    但兩者「合計」共用 max_output_bytes 上限。刻意不用 shell=True。
    """

    def __init__(self, argv, cwd, stdin_text=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES, env=None):
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("argv must be a non-empty list")
        self.argv = [str(a) for a in argv]
        self.cwd = cwd
        self.stdin_text = stdin_text
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_output_bytes = int(max_output_bytes)
        self.env = dict(env) if env is not None else build_child_env()
        self.pid = None
        self.spawn_error = False
        self.spawn_failure_kind = "spawn_error"
        self._started_mono = None
        self._deadline_mono = None
        self.started_at = None
        self.deadline = None
        self.kill_reason = None
        self.job_assigned = False
        self._p = None
        self._job = None
        self._pgid = None
        self._lock = threading.Lock()
        self._out = []
        self._err = []
        self._n = 0
        self._overflow = False
        self._threads = []
        self._finished = None

    # -- 啟動 --------------------------------------------------------------
    def start(self):
        kwargs = {"cwd": self.cwd, "env": self.env, "stdin": subprocess.PIPE,
                  "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "close_fds": True}
        self.started_at = time.time()
        self._started_mono = time.monotonic()
        self.deadline = self.started_at + self.timeout_seconds
        self._deadline_mono = self._started_mono + self.timeout_seconds
        if IS_WINDOWS:
            self._job = _win_create_kill_on_close_job()
            if not self._job:
                self.spawn_error = True
                self.spawn_failure_kind = "unsafe_environment"
                return self
            kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        try:
            self._p = subprocess.Popen(self.argv, **kwargs)
        except Exception:
            if self._job:
                _win_close(self._job)
                self._job = None
            self.spawn_error = True
            self._p = None
            return self
        self.pid = self._p.pid
        if IS_WINDOWS:
            if not _win_assign(self._job, self._p._handle):
                self._reject_suspended_child()
                return self
            self.job_assigned = True
            if not _win_resume(self._p._handle):
                self._reject_suspended_child()
                return self
        else:
            self._pgid = self._p.pid
        self._threads = [
            threading.Thread(target=self._pump, args=(self._p.stdout, self._out), daemon=True),
            threading.Thread(target=self._pump, args=(self._p.stderr, self._err), daemon=True),
            threading.Thread(target=self._write_stdin, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def _reject_suspended_child(self):
        # No uncontained child code has executed: assignment was attempted while suspended.
        self.spawn_error = True
        self.spawn_failure_kind = "unsafe_environment"
        if self._job and self.job_assigned:
            _win_kill_job(self._job)
        try:
            self._p.kill()
            self._p.wait(timeout=5)
        except Exception:
            pass
        for stream in (self._p.stdin, self._p.stdout, self._p.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if self._job:
            _win_close(self._job)
            self._job = None
        self._p = None

    def _write_stdin(self):
        try:
            if self.stdin_text:
                self._p.stdin.write(self.stdin_text.encode("utf-8", "replace"))
        except Exception:
            pass
        finally:
            try:
                self._p.stdin.close()
            except Exception:
                pass

    def _pump(self, stream, buf):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                with self._lock:
                    room = self.max_output_bytes - self._n
                    if room <= 0:
                        self._overflow = True
                        break
                    if len(chunk) > room:
                        buf.append(chunk[:room])
                        self._n += room
                        self._overflow = True
                        break
                    buf.append(chunk)
                    self._n += len(chunk)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    # -- 狀態 --------------------------------------------------------------
    def alive(self):
        return self._p is not None and self._p.poll() is None

    def stdout_text(self):
        with self._lock:
            return b"".join(self._out).decode("utf-8", "replace")

    def stderr_text(self):
        with self._lock:
            return b"".join(self._err).decode("utf-8", "replace")

    def poll(self):
        """非阻塞；回 None 代表還在跑，否則回 FinishedRaw。"""
        if self._finished is not None:
            return self._finished
        if self._p is None:
            self._finished = FinishedRaw(None, "", "", False, False, 0, 0)
            return self._finished
        rc = self._p.poll()
        now = time.monotonic()
        if rc is None and self._overflow:
            self.kill_reason = "output_overflow"
            self.terminate_tree()
            rc = self._wait_short()
        elif rc is None and now >= self._deadline_mono:
            self.kill_reason = "timeout"
            self.terminate_tree()
            rc = self._wait_short()
        if rc is None:
            return None
        for t in self._threads:
            t.join(1.5)
        self._ensure_tree_stopped()
        with self._lock:
            captured = self._n
            overflow = self._overflow
        self._finished = FinishedRaw(
            exit_code=rc,
            stdout_text=self.stdout_text(),
            stderr_text=self.stderr_text(),
            timed_out=self.kill_reason == "timeout",
            overflow=overflow or self.kill_reason == "output_overflow",
            duration_ms=int(max(0.0, time.monotonic() - self._started_mono) * 1000),
            bytes_captured=captured,
        )
        return self._finished

    def _wait_short(self):
        try:
            return self._p.wait(timeout=8)
        except Exception:
            return self._p.poll()

    # -- 收尾 --------------------------------------------------------------
    def terminate_tree(self):
        if self._p is None:
            return
        if IS_WINDOWS:  # pragma: no cover - 平台相依
            killed = _win_kill_job(self._job) if self._job else False
            if not killed and self.pid:
                _taskkill(self.pid)
        else:  # pragma: no cover - 平台相依
            self._unix_signal(signal.SIGKILL)
        try:
            self._p.kill()
        except Exception:
            pass

    def _unix_signal(self, sig):  # pragma: no cover - 平台相依
        """用「啟動時記下的」pgid 殺整組；父行程已死也有效。"""
        if self._pgid:
            try:
                os.killpg(self._pgid, sig)
                return True
            except Exception:
                pass
        try:
            os.kill(self._p.pid, sig)
            return True
        except Exception:
            return False

    def _ensure_tree_stopped(self):
        """父行程正常結束也要確保它生的孩子不留著。"""
        if IS_WINDOWS:  # pragma: no cover - 平台相依
            if self._job:
                _win_close(self._job)   # kill-on-close：關掉就收乾淨
                self._job = None
        else:  # pragma: no cover - 平台相依
            self._unix_signal(signal.SIGTERM)

    def close(self):
        if self.alive():
            self.terminate_tree()
        self._ensure_tree_stopped()


# --------------------------------------------------- 自有暫存目錄「只回收」
#
# 本區塊是使用者政策的硬界線：絕不永久刪除。
# Windows：IFileOperation + FOFX_RECYCLEONDELETE（真正的回收桶 API），
#          外加嚴格前置檢查（短路徑、固定磁碟、NTFS、$Recycle.Bin 存在、
#          NukeOnDelete/NoRecycleFiles 政策未開），以及回收桶 $I 中介資料讀回。
# Unix  ：freedesktop Trash（只用 os.rename 搬移 + 寫 .trashinfo），不可用就保留。
# 任何檢查不過 -> 目錄原樣保留 + 一句精簡警告。沒有任何永久刪除路徑。

MAX_RECYCLE_ENTRIES = 2000
RECYCLE_PATH_MAX = 200           # 刻意遠低於 MAX_PATH(260)，避免長路徑讓殼層改走永久刪除
_DRIVE_FIXED = 3


class RecycleResult(object):
    __slots__ = ("ok", "warning", "method", "metadata_verified")

    def __init__(self, ok, warning="", method="none", metadata_verified=False):
        self.ok = bool(ok)
        self.warning = warning
        self.method = method
        self.metadata_verified = bool(metadata_verified)

    def __iter__(self):           # 相容 (ok, warning) 解包
        return iter((self.ok, self.warning))


def _is_reparse_or_link(path):
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def path_contained(child, root):
    try:
        c = os.path.realpath(child)
        r = os.path.realpath(root)
    except OSError:
        return False
    if c == r:
        return False
    return os.path.normcase(c).startswith(os.path.normcase(r) + os.sep)


def scan_owned_children(target, root):
    """列出自有目錄下的全部子項；遇到連結／越界／未知型別／超長路徑就拒絕。

    回 (entries, reason)。reason 非空代表拒絕，entries 為 None。
    """
    entries = []
    stack = [target]
    count = 0
    while stack:
        cur = stack.pop()
        try:
            listed = list(os.scandir(cur))
        except OSError:
            return None, "清理略過：無法列出自有暫存目錄"
        for ent in listed:
            count += 1
            if count > MAX_RECYCLE_ENTRIES:
                return None, "清理略過：暫存項目數超過上限"
            if len(os.path.abspath(ent.path)) > RECYCLE_PATH_MAX:
                return None, "清理略過：暫存路徑過長（可能導致永久刪除）"
            if not path_contained(ent.path, root):
                return None, "清理略過：出現越界路徑"
            if _is_reparse_or_link(ent.path):
                return None, "清理略過：暫存目錄內含連結／重解析點"
            if ent.is_dir(follow_symlinks=False):
                entries.append(ent.path)
                stack.append(ent.path)
            elif ent.is_file(follow_symlinks=False):
                entries.append(ent.path)
            else:
                return None, "清理略過：暫存目錄內含未知型別項目"
    return entries, ""


# ---- Windows：IFileOperation（ctypes COM，不依賴 pywin32/.NET）

_CLSID_FileOperation = "{3AD05575-8857-4850-9277-11B85BDB8E09}"
_IID_IFileOperation = "{947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8}"
_IID_IShellItem = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_NOCONFIRMMKDIR = 0x0200
_FOF_NOERRORUI = 0x0400
_FOFX_EARLYFAILURE = 0x00100000
_FOFX_RECYCLEONDELETE = 0x00080000
_RECYCLE_FLAGS = (_FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOCONFIRMMKDIR | _FOF_NOERRORUI
                  | _FOFX_EARLYFAILURE | _FOFX_RECYCLEONDELETE)

_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = -2147417850
_S_OK = 0
_S_FALSE = 1


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _com_dlls():  # pragma: no cover - 平台相依
    from ctypes import wintypes
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
    ole32.CLSIDFromString.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
                                       ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                    ctypes.POINTER(_GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    return ole32, shell32


def _guid_from(ole32, text):  # pragma: no cover - 平台相依
    g = _GUID()
    if ole32.CLSIDFromString(text, ctypes.byref(g)) != _S_OK:
        raise OSError("CLSIDFromString failed")
    return g


def _vcall(ptr, index, argtypes, *args):  # pragma: no cover - 平台相依
    vtbl = ctypes.cast(ctypes.c_void_p(ptr),
                       ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(ctypes.c_void_p(ptr), *args)


def _release(ptr):  # pragma: no cover - 平台相依
    if ptr:
        try:
            _vcall(ptr, 2, [])
        except Exception:
            pass


def _win_volume_guid(drive):  # pragma: no cover - 平台相依
    from ctypes import wintypes
    buf = ctypes.create_unicode_buffer(64)
    try:
        if _k32().GetVolumeNameForVolumeMountPointW(drive + "\\", buf, wintypes.DWORD(64)):
            name = buf.value          # \\?\Volume{GUID}\
            i, j = name.find("{"), name.find("}")
            if 0 <= i < j:
                return name[i:j + 1]
    except Exception:
        pass
    return None


def _win_recycle_disabled_reason(drive):  # pragma: no cover - 平台相依
    """檢查「回收即刪除」政策；開著就必須拒絕回收（否則會變永久刪除）。"""
    try:
        import winreg
    except ImportError:
        return "清理略過：無法檢查回收桶政策"

    def _dw(root, path, name):
        try:
            with winreg.OpenKey(root, path) as key:
                val, typ = winreg.QueryValueEx(key, name)
                if typ in (winreg.REG_DWORD, winreg.REG_BINARY) and isinstance(val, int):
                    return val
                if isinstance(val, int):
                    return val
        except OSError:
            return None
        return None

    pol = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        if _dw(root, pol, "NoRecycleFiles") == 1:
            return "清理略過：系統政策停用回收桶"
    bucket = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
    use_global = _dw(winreg.HKEY_CURRENT_USER, bucket, "UseGlobalSettings")
    if _dw(winreg.HKEY_CURRENT_USER, bucket, "NukeOnDelete") == 1:
        return "清理略過：回收桶設定為直接刪除"
    if use_global != 1:
        vol = _win_volume_guid(drive)
        if vol:
            vol_key = bucket + "\\Volume\\" + vol
            if _dw(winreg.HKEY_CURRENT_USER, vol_key, "NukeOnDelete") == 1:
                return "清理略過：本磁碟回收桶設定為直接刪除"
    return ""


def _win_recycle_preflight(target):  # pragma: no cover - 平台相依
    """短路徑 / 本機固定 NTFS / $Recycle.Bin 可用 / 政策允許。"""
    from ctypes import wintypes
    abs_target = os.path.abspath(target)
    if abs_target.startswith("\\\\") or abs_target.startswith("\\\\?\\"):
        return "清理略過：UNC／擴充長路徑不回收"
    if len(abs_target) > RECYCLE_PATH_MAX:
        return "清理略過：暫存路徑過長（可能導致永久刪除）"
    drive = os.path.splitdrive(abs_target)[0]
    if not re.match(r"^[A-Za-z]:$", drive or ""):
        return "清理略過：無法判定本機磁碟"
    try:
        if _k32().GetDriveTypeW(drive + "\\") != _DRIVE_FIXED:
            return "清理略過：非本機固定磁碟（無回收桶）"
        fs = ctypes.create_unicode_buffer(32)
        ok = _k32().GetVolumeInformationW(drive + "\\", None, 0, None, None, None,
                                          fs, wintypes.DWORD(32))
        if not ok or (fs.value or "").upper() != "NTFS":
            return "清理略過：檔案系統非 NTFS（無回收桶）"
    except Exception:
        return "清理略過：無法確認磁碟與檔案系統"
    if not os.path.isdir(os.path.join(drive + "\\", "$Recycle.Bin")):
        return "清理略過：找不到 $Recycle.Bin"
    return _win_recycle_disabled_reason(drive)


def _decode_recycle_info(blob):
    """解析回收桶 $I 中介資料，取出原始路徑（v1 固定 260 字、v2 帶長度）。"""
    if len(blob) < 28:
        return None
    try:
        version = struct.unpack("<Q", blob[0:8])[0]
    except struct.error:
        return None
    if version == 1:
        raw = blob[24:24 + 520]
    elif version == 2:
        n = struct.unpack("<I", blob[24:28])[0]
        if n <= 0 or n > 4096:
            return None
        raw = blob[28:28 + n * 2]
    else:
        return None
    try:
        text = raw.decode("utf-16-le", "ignore")
    except Exception:
        return None
    return text.split("\x00", 1)[0] or None


def _current_user_sid():
    """Read only the current process token SID; never inventory other users."""
    token = ctypes.c_void_p()
    sid_text = ctypes.c_void_p()
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        adv.OpenProcessToken.restype = ctypes.c_int
        adv.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                             ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        adv.GetTokenInformation.restype = ctypes.c_int
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        adv.ConvertSidToStringSidW.restype = ctypes.c_int
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None
        needed = ctypes.c_uint()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not (0 < needed.value <= 65536):
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            return None
        value = ctypes.wstring_at(sid_text)
        return value if re.fullmatch(r"S-1-[0-9-]+", value) else None
    except Exception:
        return None
    finally:
        if sid_text.value:
            kernel.LocalFree(sid_text)
        if token.value:
            kernel.CloseHandle(token)


def _win_recycle_metadata_verified(abs_target, since):
    """Read exact current-user $I metadata and match only our owned original path."""
    sid = _current_user_sid()
    if not sid:
        return False
    drive = os.path.splitdrive(abs_target)[0]
    owned_bin = os.path.join(drive + "\\", "$Recycle.Bin", sid)
    want = os.path.normcase(abs_target)
    try:
        items = os.scandir(owned_bin)
    except OSError:
        return False
    checked = 0
    with items:
        for entry in items:
            if not entry.name.lower().startswith("$i"):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < since - 10:
                    continue
                checked += 1
                if checked > 400:
                    return False
                with open(entry.path, "rb") as handle:
                    original_path = _decode_recycle_info(handle.read(4096))
            except OSError:
                continue
            if original_path and os.path.normcase(original_path) == want:
                return True
    return False


def _win_recycle(abs_target):  # pragma: no cover - 平台相依
    """用 IFileOperation + FOFX_RECYCLEONDELETE 回收單一自有目錄。"""
    try:
        ole32, shell32 = _com_dlls()
    except Exception:
        return False, "清理略過：無法載入回收桶 COM 介面", False
    hr_init = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if hr_init not in (_S_OK, _S_FALSE, _RPC_E_CHANGED_MODE):
        return False, "清理略過：COM 初始化失敗", False
    need_uninit = hr_init in (_S_OK, _S_FALSE)
    pfo = ctypes.c_void_p()
    psi = ctypes.c_void_p()
    try:
        clsid = _guid_from(ole32, _CLSID_FileOperation)
        iid_op = _guid_from(ole32, _IID_IFileOperation)
        iid_si = _guid_from(ole32, _IID_IShellItem)
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                  ctypes.byref(iid_op), ctypes.byref(pfo)) != _S_OK or not pfo:
            return False, "清理略過：無法建立 IFileOperation", False
        if _vcall(pfo.value, 5, [ctypes.c_ulong], ctypes.c_ulong(_RECYCLE_FLAGS)) != _S_OK:
            return False, "清理略過：無法設定回收旗標", False
        if shell32.SHCreateItemFromParsingName(abs_target, None, ctypes.byref(iid_si),
                                               ctypes.byref(psi)) != _S_OK or not psi:
            return False, "清理略過：無法取得暫存目錄的 shell 項目", False
        since = time.time()
        if _vcall(pfo.value, 18, [ctypes.c_void_p, ctypes.c_void_p],
                  psi, None) != _S_OK:
            return False, "清理略過：回收佇列建立失敗", False
        if _vcall(pfo.value, 21, []) != _S_OK:
            return False, "清理未完成：回收操作失敗，目錄保留", False
        aborted = ctypes.c_int(0)
        if _vcall(pfo.value, 22, [ctypes.POINTER(ctypes.c_int)],
                  ctypes.byref(aborted)) != _S_OK or aborted.value:
            return False, "清理未完成：回收被中止，目錄保留", False
        if os.path.exists(abs_target):
            return False, "清理未完成：目錄仍在原處，保留不動", False
        verified = _win_recycle_metadata_verified(abs_target, since)
        if not verified:
            return True, "清理完成但未讀回回收桶中介資料", False
        return True, "", True
    except Exception:
        return False, "清理略過：回收桶介面異常，目錄保留", False
    finally:
        _release(psi.value if psi else None)
        _release(pfo.value if pfo else None)
        if need_uninit:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


# ---- Unix：freedesktop Trash（只搬移，不 unlink）

def _unix_trash(abs_target):  # pragma: no cover - 平台相依
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if not home or not os.path.isdir(home):
        return False, "清理略過：找不到家目錄，無法移入垃圾桶", False
    base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"),
                        "Trash")
    files_dir, info_dir = os.path.join(base, "files"), os.path.join(base, "info")
    try:
        os.makedirs(files_dir, exist_ok=True)
        os.makedirs(info_dir, exist_ok=True)
    except OSError:
        return False, "清理略過：無法準備垃圾桶目錄", False
    name = os.path.basename(abs_target.rstrip(os.sep)) or "owned"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    for i in range(50):
        cand = name if i == 0 else "%s.%d" % (name, i)
        dest = os.path.join(files_dir, cand)
        info = os.path.join(info_dir, cand + ".trashinfo")
        if os.path.lexists(dest) or os.path.lexists(info):
            continue
        try:
            with open(info, "x", encoding="utf-8") as fh:
                fh.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (abs_target, stamp))
        except OSError:
            continue
        try:
            os.rename(abs_target, dest)   # 同裝置搬移；跨裝置會失敗 -> 保留原目錄
        except OSError:
            return False, "清理略過：無法搬入垃圾桶（可能跨裝置），目錄保留", False
        return True, "", os.path.exists(dest)
    return False, "清理略過：垃圾桶名稱衝突，目錄保留", False


def recycle_owned_dir(target, root):
    """安全「回收」自有的單一工作目錄；回 RecycleResult（可解包成 (ok, warning)）。

    這裡沒有任何永久刪除路徑：檢查不過就原樣保留並回一句精簡警告。
    """
    if not target or not os.path.isdir(target):
        return RecycleResult(True, "", "noop", False)
    if not path_contained(target, root):
        return RecycleResult(False, "清理略過：目錄不在自有暫存根目錄內", "rejected")
    if _is_reparse_or_link(target):
        return RecycleResult(False, "清理略過：目標是連結／重解析點", "rejected")
    _entries, reason = scan_owned_children(target, root)
    if reason:
        return RecycleResult(False, reason, "rejected")
    abs_target = os.path.abspath(target)
    if IS_WINDOWS:  # pragma: no cover - 平台相依
        pre = _win_recycle_preflight(abs_target)
        if pre:
            return RecycleResult(False, pre, "rejected")
        ok, warn, verified = _win_recycle(abs_target)
        return RecycleResult(ok, warn, "windows_recycle_bin" if ok else "rejected", verified)
    ok, warn, verified = _unix_trash(abs_target)   # pragma: no cover - 平台相依
    return RecycleResult(ok, warn, "freedesktop_trash" if ok else "rejected", verified)


# ------------------------------------------------------------ CLI 家族規格

def resolve_launcher(path):
    """原生 .exe 優先；必要時把 npm 的 .cmd shim 解析成 node + 腳本（不走 shell）。"""
    if not path:
        return None
    low = path.lower()
    if not low.endswith((".cmd", ".bat")):
        return [path]
    node = shutil.which("node")
    if not node:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return None
    base = os.path.dirname(path)
    for m in re.finditer(r'"?%~dp0\\?([^"%\r\n]+?\.(?:js|mjs|cjs))"?', text):
        cand = os.path.normpath(os.path.join(base, m.group(1)))
        if os.path.isfile(cand):
            return [node, cand]
    for m in re.finditer(r'([A-Za-z0-9_\-./\\]+\.(?:js|mjs|cjs))', text):
        cand = os.path.normpath(os.path.join(base, m.group(1)))
        if os.path.isfile(cand):
            return [node, cand]
    return None


def _argv_claude(launcher, prompt, cwd, prompt_path):
    return launcher + [
        "--tools", "",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--model", "opus",
        "--effort", "low",
        "--output-format", "json",
        "-p", prompt,
    ]


def _argv_codex(launcher, prompt, cwd, prompt_path):
    # project_doc_max_bytes=0：不自動展開任何 AGENTS.md（避免在唯讀研究任務裡遞迴派工）
    return launcher + [
        "exec",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s", "read-only",
        "-c", "project_doc_max_bytes=0",
        "-c", "features.shell_tool=false",
        "-c", 'web_search="disabled"',
        "-c", "features.plugins=false",
        "-c", "features.remote_plugin=false",
        "-c", "mcp_servers={}",
        "-C", cwd,
        "-m", "gpt-6.1-sol",
        "-",
    ]


def _argv_grok(launcher, prompt, cwd, prompt_path):
    return launcher + [
        "--verbatim",
        "--tools", "",
        "--deny", "*",
        "--no-subagents",
        "--disable-web-search",
        "--no-plan",
        "--max-turns", "1",
        "--permission-mode", "dontAsk",
        "--sandbox", "read-only",
        "--output-format", "json",
        "--prompt-file", prompt_path,
    ]


def _argv_agy(launcher, prompt, cwd, prompt_path):
    # 原生 AGY CLI 沒有檔案系統／終端機工具，是純文字一次性分析，這就是它被允許的能力。
    return launcher + [
        "--sandbox",
        "--disable-slash-commands",
        "--print-timeout", "70s",
        "--model", "gemini-4-argon-medium",
        "--output-format", "json",
        "-p", prompt,
    ]


class CliSpec(object):
    """一個 CLI 家族的安全呼叫規格（core_flags 缺一不可，optional 只記錄）。"""

    def __init__(self, alias, exe, configured_model, help_argv, core_flags, optional_flags,
                 argv_builder, uses_stdin=False, uses_prompt_file=False):
        self.alias = alias
        self.exe = exe
        self.configured_model = configured_model
        self.help_argv = help_argv
        self.core_flags = core_flags
        self.optional_flags = optional_flags
        self.argv_builder = argv_builder
        self.uses_stdin = uses_stdin
        self.uses_prompt_file = uses_prompt_file


# configured_model=None 代表「原生預設、未鎖定」，標籤必須是 UNVERIFIED
SPEC_TEMPLATES = {
    "CLAUDE": dict(exe="claude", configured_model="opus", help_argv=["--help"],
                   core_flags=("--strict-mcp-config", "--mcp-config", "--disable-slash-commands",
                               "--permission-mode", "--output-format"),
                   optional_flags=("--tools", "--setting-sources", "--no-session-persistence",
                                   "--effort", "--model"),
                   argv_builder=_argv_claude),
    "CODEX": dict(exe="codex", configured_model="gpt-6.1-sol", help_argv=["exec", "--help"],
                  core_flags=("--ignore-user-config", "--ignore-rules", "--ephemeral",
                              "--skip-git-repo-check", "--sandbox", "--json"),
                  optional_flags=("read-only", "--config", "-C"),
                  argv_builder=_argv_codex, uses_stdin=True),
    "GROK": dict(exe="grok", configured_model=None, help_argv=["--help"],
                 core_flags=("--tools", "--deny", "--sandbox", "--permission-mode",
                             "--output-format", "--prompt-file"),
                 optional_flags=("--verbatim", "--no-subagents", "--disable-web-search",
                                 "--no-plan", "--max-turns"),
                 argv_builder=_argv_grok, uses_prompt_file=True),
    "AGY": dict(exe="agy", configured_model="gemini-4-argon-medium", help_argv=["--help"],
                core_flags=("--sandbox", "--disable-slash-commands", "--output-format"),
                optional_flags=("--print-timeout", "--model", "-p"),
                argv_builder=_argv_agy),
}


def honest_model_label(configured, reported):
    """未經實際回傳中介資料確認前，一律標成設定值／UNVERIFIED。"""
    if isinstance(reported, str) and _MODEL_RE.match(reported.strip()):
        return reported.strip()[:80]
    if not configured:
        return "UNVERIFIED"
    return "%s（設定值未驗證）" % configured


# ---------------------------------------------------------------- 執行結果

class Outcome(object):
    __slots__ = ("status", "verdict", "finding", "evidence", "model_reported", "exit_code",
                 "failure_kind", "failure_reason", "duration_ms")

    def __init__(self, status, verdict=None, finding="", evidence=None, model_reported=None,
                 exit_code=None, failure_kind=None, failure_reason="", duration_ms=0):
        self.status = status
        self.verdict = verdict
        self.finding = finding
        self.evidence = list(evidence or [])
        self.model_reported = model_reported
        self.exit_code = exit_code
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        self.duration_ms = int(duration_ms)

    @property
    def validated(self):
        """唯一可以加 XP 的條件：結束碼 0 + 型別化 JSON 通過。"""
        return (self.status == "completed" and self.verdict in VERDICTS
                and bool(self.finding) and self.exit_code == 0)


def interpret(alias, raw):
    """把 FinishedRaw 轉成 Outcome；只有「結束碼 0 且 stdout 型別化 JSON 合規」才算完成。"""
    if raw.exit_code is None and not raw.stdout_text and not raw.stderr_text:
        return Outcome("failed", failure_kind="spawn_error",
                       failure_reason=FAILURE_TEXT["spawn_error"])
    if raw.timed_out:
        return Outcome("timed_out", exit_code=raw.exit_code, failure_kind="timeout",
                       failure_reason=FAILURE_TEXT["timeout"], duration_ms=raw.duration_ms)
    if raw.overflow:
        return Outcome("failed", exit_code=raw.exit_code, failure_kind="output_overflow",
                       failure_reason=FAILURE_TEXT["output_overflow"],
                       duration_ms=raw.duration_ms)
    # stderr 只在記憶體裡做失敗分類，絕不落地、不外曝、不當成結果
    diag = (raw.stderr_text or "") + "\n" + (raw.stdout_text or "")
    if raw.exit_code != 0:
        kind = classify_failure(diag, raw.exit_code, default="nonzero_exit")
        return Outcome("failed", exit_code=raw.exit_code, failure_kind=kind,
                       failure_reason=FAILURE_TEXT[kind], duration_ms=raw.duration_ms)
    try:
        payload, model = parse_cli_output(alias, raw.stdout_text)
    except ParseError as exc:
        kind = classify_failure(diag, raw.exit_code, default=exc.kind)
        return Outcome("failed", exit_code=raw.exit_code, failure_kind=kind,
                       failure_reason=FAILURE_TEXT[kind], duration_ms=raw.duration_ms)
    return Outcome("completed", verdict=payload["verdict"], finding=payload["finding"],
                   evidence=payload["evidence"], model_reported=model,
                   exit_code=raw.exit_code, duration_ms=raw.duration_ms)


# ------------------------------------------------------------- 真實執行器

class JobHandle(object):
    """排程器看到的工作把手：只能查詢與終止，不能改內容。"""

    def __init__(self, proc, job_dir, temp_root, alias):
        self._proc = proc
        self.job_dir = job_dir
        self.alias = alias
        self._temp_root = temp_root
        self.cleanup_warning = ""
        self.cleanup_method = ""
        self._outcome = None

    @property
    def pid(self):
        return self._proc.pid

    def alive(self):
        return self._proc.alive()

    def _recycle(self):
        res = recycle_owned_dir(self.job_dir, self._temp_root)
        self.cleanup_method = res.method
        if not res.ok or res.warning:
            self.cleanup_warning = res.warning or "清理未完成：目錄保留"
        return res

    def poll(self):
        if self._outcome is not None:
            return self._outcome
        raw = self._proc.poll()
        if raw is None:
            return None
        self._outcome = interpret(self.alias, raw)
        self._recycle()
        return self._outcome

    def terminate(self):
        self._proc.terminate_tree()

    def close(self):
        self._proc.close()
        self._recycle()


class NativeCliRunner(object):
    """真的去跑已安裝的原生 CLI。探測只做 PATH / --help，不讀認證檔、不做計費備援。"""

    def __init__(self, temp_root, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES, help_timeout=HELP_PROBE_TIMEOUT):
        self.temp_root = os.path.abspath(temp_root)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.help_timeout = help_timeout
        self._discovery = None
        os.makedirs(self.temp_root, exist_ok=True)

    # -- 探測 --------------------------------------------------------------
    def _probe_help(self, launcher, help_argv):
        """Discovery has the same process-tree, deadline and output bounds as jobs."""
        process = OwnedProcess(launcher + list(help_argv), cwd=self.temp_root,
                               timeout_seconds=self.help_timeout,
                               max_output_bytes=self.max_output_bytes,
                               env=build_child_env()).start()
        try:
            if process.spawn_error:
                return ""
            while True:
                raw = process.poll()
                if raw is not None:
                    if raw.timed_out or raw.overflow or raw.exit_code != 0:
                        return ""
                    return (raw.stdout_text + "\n" + raw.stderr_text)[:self.max_output_bytes]
                time.sleep(0.01)
        finally:
            process.close()

    def discover(self, refresh=False):
        """回 {alias: {available, reason, model, launcher, notes}}。結果快取。"""
        if self._discovery is not None and not refresh:
            return self._discovery
        found = {}
        for alias in DISABLED_ALIASES:
            found[alias] = {"available": False, "disabled": True, "launcher": None,
                            "model": "—", "notes": "",
                            "reason": DISABLED_REASON.get(alias, "已關閉")}
        for alias in ALIAS_ORDER:
            tpl = SPEC_TEMPLATES[alias]
            label = honest_model_label(tpl["configured_model"], None)
            base = {"available": False, "disabled": False, "launcher": None,
                    "model": label, "notes": ""}
            path = shutil.which(tpl["exe"])
            if not path:
                found[alias] = dict(base, reason="找不到 %s 執行檔" % tpl["exe"])
                continue
            launcher = resolve_launcher(path)
            if not launcher:
                found[alias] = dict(base, reason="無法在不經 shell 的情況下解析啟動器")
                continue
            help_text = self._probe_help(launcher, tpl["help_argv"])
            if not help_text.strip():
                found[alias] = dict(base, reason="--help 無輸出，缺少能力證據")
                continue
            missing = [f for f in tpl["core_flags"] if f not in help_text]
            if missing:
                found[alias] = dict(base, reason="缺少安全旗標證據：%s" % " ".join(missing[:4]))
                continue
            soft = [f for f in tpl["optional_flags"] if f not in help_text]
            found[alias] = dict(base, available=True, launcher=launcher, reason="",
                                notes=("--help 未列出：%s" % " ".join(soft[:4])) if soft else "")
        self._discovery = found
        return found

    def configured_model(self, alias):
        tpl = SPEC_TEMPLATES.get(alias)
        return tpl["configured_model"] if tpl else None

    # -- 啟動 --------------------------------------------------------------
    def start(self, alias, job_id, prompt):
        """啟動一個真實子行程；成功回 JobHandle，失敗回 Outcome(failed)。"""
        info = self.discover().get(alias) or {}
        if not info.get("available"):
            return Outcome("failed", failure_kind="unavailable",
                           failure_reason=info.get("reason") or FAILURE_TEXT["unavailable"])
        tpl = SPEC_TEMPLATES[alias]
        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "", str(job_id))[:48] or "job"
        job_dir = os.path.join(self.temp_root, safe_id)
        try:
            os.makedirs(job_dir, exist_ok=True)
        except OSError:
            return Outcome("failed", failure_kind="spawn_error",
                           failure_reason=FAILURE_TEXT["spawn_error"])
        prompt_path = os.path.join(job_dir, "prompt.txt")
        if tpl.get("uses_prompt_file"):
            try:
                with open(prompt_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(prompt)
            except OSError:
                recycle_owned_dir(job_dir, self.temp_root)
                return Outcome("failed", failure_kind="spawn_error",
                               failure_reason=FAILURE_TEXT["spawn_error"])
        argv = tpl["argv_builder"](info["launcher"], prompt, job_dir, prompt_path)
        proc = OwnedProcess(argv, cwd=job_dir,
                            stdin_text=prompt if tpl.get("uses_stdin") else None,
                            timeout_seconds=self.timeout_seconds,
                            max_output_bytes=self.max_output_bytes).start()
        if proc.spawn_error:
            recycle_owned_dir(job_dir, self.temp_root)
            kind = getattr(proc, "spawn_failure_kind", "spawn_error")
            return Outcome("failed", failure_kind=kind,
                           failure_reason=FAILURE_TEXT.get(kind, FAILURE_TEXT["spawn_error"]))
        return JobHandle(proc, job_dir, self.temp_root, alias)
