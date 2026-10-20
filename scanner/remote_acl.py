# -*- coding: utf-8 -*-
"""Windows owner-only DACL enforcement using stdlib ctypes."""
import ctypes, os

if os.name == "nt": from ctypes import wintypes
else: wintypes = None

SID_EVERYONE = 1; SID_LOCAL_SYSTEM = 22
_ERROR_SUCCESS = 0; _SE_FILE_OBJECT = 1
_OWNER_INFO = 1; _DACL_INFO = 4; _PROTECTED_DACL_INFO = 0x80000000; _SE_DACL_PROTECTED = 0x1000
_ACL_REVISION = 2; _ACCESS_ALLOWED_ACE_TYPE = 0; _INHERIT_ALL = 3; _FILE_ALL_ACCESS = 0x001F01FF
_TOKEN_QUERY = 8; _TOKEN_USER = 1; STORE_TRUSTEES = (SID_LOCAL_SYSTEM,)


class _ACL(ctypes.Structure):
    _fields_ = [("AclRevision", ctypes.c_ubyte), ("Sbz1", ctypes.c_ubyte), ("AclSize", ctypes.c_ushort), ("AceCount", ctypes.c_ushort), ("Sbz2", ctypes.c_ushort)]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte), ("AceSize", ctypes.c_ushort)]


_ACE_SID_OFFSET = 8
_STATE = {}

def _api():
    if "api" in _STATE:
        return _STATE["api"]
    result = None
    if os.name == "nt":
        try:
            adv = ctypes.WinDLL("advapi32", use_last_error=True)
            ker = ctypes.WinDLL("kernel32", use_last_error=True)
            pacl = ctypes.POINTER(_ACL)
            void, dword = ctypes.c_void_p, wintypes.DWORD
            specs = (
                ("GetNamedSecurityInfoW", dword, [wintypes.LPCWSTR, ctypes.c_int, dword, ctypes.POINTER(void), ctypes.POINTER(void), ctypes.POINTER(pacl), ctypes.POINTER(pacl), ctypes.POINTER(void)]),
                ("SetNamedSecurityInfoW", dword, [wintypes.LPWSTR, ctypes.c_int, dword, void, void, pacl, pacl]),
                ("GetSecurityDescriptorControl", wintypes.BOOL, [void, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(dword)]),
                ("InitializeAcl", wintypes.BOOL, [pacl, dword, dword]),
                ("AddAccessAllowedAceEx", wintypes.BOOL, [pacl, dword, dword, dword, void]),
                ("GetAce", wintypes.BOOL, [pacl, dword, ctypes.POINTER(void)]),
                ("EqualSid", wintypes.BOOL, [void, void]), ("IsValidSid", wintypes.BOOL, [void]),
                ("GetLengthSid", dword, [void]),
                ("CreateWellKnownSid", wintypes.BOOL, [ctypes.c_int, void, void, ctypes.POINTER(dword)]),
                ("OpenProcessToken", wintypes.BOOL, [wintypes.HANDLE, dword, ctypes.POINTER(wintypes.HANDLE)]),
                ("GetTokenInformation", wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, void, dword, ctypes.POINTER(dword)]))
            for name, restype, argtypes in specs:
                fn = getattr(adv, name)
                fn.restype, fn.argtypes = restype, argtypes
            ker.LocalFree.restype, ker.LocalFree.argtypes = void, [void]
            ker.GetCurrentProcess.restype = wintypes.HANDLE
            ker.CloseHandle.argtypes = [wintypes.HANDLE]
            result = adv, ker
        except (OSError, AttributeError, ValueError):
            pass
    _STATE["api"] = result
    return result

def available():
    try: return _api() is not None and _current_user_sid() is not None and _well_known(SID_LOCAL_SYSTEM) is not None
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError): return False

def _ptr(sid): return ctypes.cast(sid, ctypes.c_void_p)

def _copy_sid(address):
    adv = _api()[0]
    if not address or not adv.IsValidSid(ctypes.c_void_p(address)):
        return None
    length = int(adv.GetLengthSid(ctypes.c_void_p(address)))
    if not 8 <= length <= 256:
        return None
    out = ctypes.create_string_buffer(length)
    ctypes.memmove(out, ctypes.c_void_p(address), length)
    return out

def _current_user_sid():
    if "user" in _STATE:
        return _STATE["user"]
    api, sid = _api(), None
    if api is not None:
        adv, ker = api
        token = wintypes.HANDLE()
        if adv.OpenProcessToken(ker.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            try:
                size = wintypes.DWORD(0)
                adv.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
                buf = ctypes.create_string_buffer(max(size.value, 16))
                if adv.GetTokenInformation(token, _TOKEN_USER, buf, size.value, ctypes.byref(size)):
                    sid = _copy_sid(ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0])
            finally:
                ker.CloseHandle(token)
    _STATE["user"] = sid
    return sid

def _well_known(kind):
    cache = _STATE.setdefault("wk", {})
    if kind in cache:
        return cache[kind]
    api, sid = _api(), None
    if api is not None:
        size = wintypes.DWORD(0)
        api[0].CreateWellKnownSid(kind, None, None, ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 16))
        if api[0].CreateWellKnownSid(kind, None, buf, ctypes.byref(size)):
            sid = buf
    cache[kind] = sid
    return sid

def _trustees(kinds):
    out = [_current_user_sid()] + [_well_known(kind) for kind in kinds]
    return out if all(sid is not None for sid in out) else None

def _apply_dacl(path, sids, inherit=False, protected=True, set_owner=True):
    api = _api()
    if api is None or not sids:
        return False
    adv = api[0]
    size = ctypes.sizeof(_ACL) + sum(12 + int(adv.GetLengthSid(_ptr(s))) for s in sids)
    acl = ctypes.cast(ctypes.create_string_buffer(size), ctypes.POINTER(_ACL))
    if not adv.InitializeAcl(acl, size, _ACL_REVISION):
        return False
    flags = _INHERIT_ALL if inherit else 0
    for sid in sids:
        if not adv.AddAccessAllowedAceEx(acl, _ACL_REVISION, flags, _FILE_ALL_ACCESS, _ptr(sid)):
            return False
    info = _DACL_INFO | (_PROTECTED_DACL_INFO if protected else 0)
    name = ctypes.create_unicode_buffer(path)
    if set_owner and adv.SetNamedSecurityInfoW(name, _SE_FILE_OBJECT, info | _OWNER_INFO,
                                               _ptr(sids[0]), None, acl, None) == _ERROR_SUCCESS:
        return True
    return adv.SetNamedSecurityInfoW(name, _SE_FILE_OBJECT, info, None, None,
                                     acl, None) == _ERROR_SUCCESS

def _inspect(path):
    api = _api()
    if api is None:
        return False
    adv, ker = api
    allowed = _trustees(STORE_TRUSTEES)
    if allowed is None:
        return False
    psd, owner, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.POINTER(_ACL)()
    if adv.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _OWNER_INFO | _DACL_INFO,
                                 ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                 ctypes.byref(psd)) != _ERROR_SUCCESS:
        return False
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not adv.GetSecurityDescriptorControl(psd, ctypes.byref(control), ctypes.byref(revision)):
            return False
        if not (control.value & _SE_DACL_PROTECTED):
            return False
        if not owner.value or not adv.EqualSid(owner, _ptr(allowed[0])):
            return False
        if not dacl:                        # NULL DACL 等於所有人全權
            return False
        user_seen = False
        for index in range(dacl.contents.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                return False
            header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            if header.AceType != _ACCESS_ALLOWED_ACE_TYPE or header.AceSize < 12:
                return False
            sid = ctypes.c_void_p(ace.value + _ACE_SID_OFFSET)
            if not adv.IsValidSid(sid):
                return False
            is_user = bool(adv.EqualSid(sid, _ptr(allowed[0])))
            if not any(adv.EqualSid(sid, _ptr(s)) for s in allowed):
                return False
            user_seen = user_seen or is_user
        return user_seen
    finally:
        if psd.value:
            ker.LocalFree(psd)

def harden(path, directory=False):
    try:
        sids = _trustees(STORE_TRUSTEES); return bool(sids and _apply_dacl(path, sids, inherit=directory) and _inspect(path))
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError): return False

def is_private(path):
    try: return _inspect(path)
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError): return False

def parent_is_safe(path):
    return is_private(path)
