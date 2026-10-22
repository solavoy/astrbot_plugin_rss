from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


class CookieManager:
    """Private per-origin browser cookie store with strict client binding."""

    _active_acl = False

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "site_sessions"
        self.profile_root = self.root.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_root.mkdir(parents=True, exist_ok=True)
        for directory in (self.profile_root, self.root):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        if sys.platform == "win32":
            try:
                icacls = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe")
                username = subprocess.run(
                    ["whoami"], capture_output=True, text=True, check=True, timeout=5
                ).stdout.strip()
                results = [
                    subprocess.run(
                        [icacls, str(directory), "/inheritance:r", "/grant:r", f"{username}:(OI)(CI)F"],
                        capture_output=True,
                        timeout=10,
                    )
                    for directory in (self.profile_root, self.root)
                ]
                CookieManager._active_acl = all(result.returncode == 0 for result in results)
            except (OSError, subprocess.SubprocessError):
                CookieManager._active_acl = False

    @staticmethod
    def _key(origin: str) -> str:
        from hashlib import sha256

        return sha256(origin.encode()).hexdigest()

    def _path(self, origin: str) -> Path:
        return self.root / f"{self._key(origin)}.json"

    def stored_origin_for_url(self, url: str) -> str:
        parts = urlsplit(url)
        labels = (parts.hostname or "").lower().split(".")
        candidates = [f"{parts.scheme}://{parts.netloc}"] + [
            f"{parts.scheme}://{'.'.join(labels[i:])}" for i in range(1, len(labels) - 1)
        ]
        return next((candidate for candidate in candidates if self._path(candidate).exists()), candidates[0])

    @staticmethod
    def _protect(data: bytes) -> bytes:
        if sys.platform != "win32":
            return data

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source = Blob(len(data), source_buffer)
        target = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        protect = crypt32.CryptProtectData
        protect.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_wchar_p,
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(Blob),
        ]
        protect.restype = ctypes.c_int
        if not protect(ctypes.byref(source), "AstrBot RSS cookies", None, None, None, 0x1, ctypes.byref(target)):
            if CookieManager._active_acl:
                return data
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return b"DPAPI1\n" + ctypes.string_at(target.pbData, target.cbData)
        finally:
            ctypes.WinDLL("kernel32").LocalFree(target.pbData)

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        if not data.startswith(b"DPAPI1\n"):
            return data
        if sys.platform != "win32":
            raise ValueError("DPAPI cookie file belongs to another platform user")

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        raw = data[7:]
        source_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        source = Blob(len(raw), source_buffer)
        target = Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(Blob),
        ]
        unprotect.restype = ctypes.c_int
        if not unprotect(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            ctypes.WinDLL("kernel32").LocalFree(target.pbData)

    @classmethod
    def _read_json(cls, path: Path) -> dict:
        return json.loads(cls._unprotect(path.read_bytes()).decode("utf-8"))

    @classmethod
    def _write_json(cls, path: Path, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(cls._protect(raw))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def save(
        self,
        origin: str,
        cookies: list[dict],
        user_agent: str,
        proxy_profile_id: str,
        browser_version: str,
        impersonate: str,
        status: str = "browser_obtained",
        browser_metadata: dict | None = None,
    ) -> None:
        host = urlsplit(origin).hostname or ""
        clean: list[dict] = []
        for cookie in cookies:
            raw_domain = str(cookie.get("domain", host)).lower()
            domain = raw_domain.lstrip(".")
            if (
                not (host.lower() == domain or host.lower().endswith("." + domain))
                or not cookie.get("name")
                or "value" not in cookie
            ):
                continue
            expires = cookie.get("expires")
            if expires not in (None, -1, 0, "") and float(expires) <= time.time():
                continue
            item = {
                k: cookie.get(k)
                for k in ("name", "value", "domain", "path", "expires", "secure", "httpOnly", "sameSite")
                if k in cookie
            }
            item["hostOnly"] = not raw_domain.startswith(".")
            clean.append(item)
        payload = {
            "origin": origin,
            "cookies": clean,
            "user_agent": user_agent,
            "proxy_profile_id": proxy_profile_id,
            "browser_version": browser_version,
            "impersonate": impersonate,
            "updated_at": time.time(),
            "last_success_at": time.time(),
            "validation_status": status,
            "created_at": time.time(),
            "browser_metadata": browser_metadata or {},
        }
        path = self._path(origin)
        self._write_json(path, payload)

    def get_for_url(
        self, url: str, user_agent: str, proxy_profile_id: str, browser_version: str, impersonate: str
    ) -> list[dict]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        labels = (parts.hostname or "").lower().split(".")
        candidates = [origin] + [f"{parts.scheme}://{'.'.join(labels[i:])}" for i in range(1, len(labels) - 1)]
        data = None
        for candidate in candidates:
            try:
                data = self._read_json(self._path(candidate))
                break
            except (OSError, ValueError):
                continue
        if data is None:
            return []
        if data.get("validation_status") not in {"browser_obtained", "validated"} or any(
            data.get(key) != value
            for key, value in (
                ("user_agent", user_agent),
                ("proxy_profile_id", proxy_profile_id),
                ("browser_version", browser_version),
                ("impersonate", impersonate),
            )
        ):
            return []
        now = time.time()
        result = []
        for cookie in data.get("cookies", []):
            expires = cookie.get("expires")
            domain = str(cookie.get("domain", "")).lower()
            path = cookie.get("path") or "/"
            if expires not in (None, -1, 0, "") and float(expires) <= now:
                continue
            host = (parts.hostname or "").lower()
            domain = domain.lstrip(".")
            if cookie.get("hostOnly", False):
                domain_matches = host == domain.lstrip(".")
            else:
                domain = domain.lstrip(".")
                domain_matches = host == domain or host.endswith("." + domain)
            if not domain_matches:
                continue
            request_path = parts.path or "/"
            if not (request_path == path or request_path.startswith(path.rstrip("/") + "/")) or (
                cookie.get("secure") and parts.scheme != "https"
            ):
                continue
            result.append(cookie)
        return result

    def client_metadata_for_url(
        self,
        url: str,
        user_agent: str,
        proxy_profile_id: str,
        browser_version: str | None,
        impersonate: str,
        binary_version: str | None = None,
    ) -> dict:
        parts = urlsplit(url)
        origin = self.stored_origin_for_url(url)
        try:
            data = self._read_json(self._path(origin))
        except (OSError, ValueError):
            return {}
        if data.get("validation_status") not in {"browser_obtained", "validated"}:
            return {}
        expected = [("proxy_profile_id", proxy_profile_id), ("impersonate", impersonate)]
        if browser_version is not None:
            expected.append(("browser_version", browser_version))
        if any(data.get(key) != value for key, value in expected):
            return {}
        host = (parts.hostname or "").lower()
        domain = (urlsplit(origin).hostname or "").lower()
        if host != domain and not host.endswith("." + domain):
            return {}
        metadata = data.get("browser_metadata", {})
        if not isinstance(metadata, dict):
            return {}
        if binary_version is not None and metadata.get("binary_version") != binary_version:
            return {}
        return metadata

    def mark_invalid(self, origin: str) -> None:
        path = self._path(origin)
        try:
            data = self._read_json(path)
            data["validation_status"] = "invalid"
            data["cookies"] = []
            self._write_json(path, data)
        except (OSError, ValueError):
            pass

    def mark_invalid_for_url(self, url: str) -> None:
        self.mark_invalid(self.stored_origin_for_url(url))

    def mark_validated_for_url(self, url: str) -> None:
        self.mark_validated(self.stored_origin_for_url(url))

    def mark_validated(self, origin: str) -> None:
        path = self._path(origin)
        try:
            data = self._read_json(path)
            data["validation_status"] = "validated"
            data["last_success_at"] = time.time()
            self._write_json(path, data)
        except (OSError, ValueError):
            pass

    def _clear_profile(self, host: str) -> None:
        if not host:
            return
        profile_base = self.root.parent.resolve()
        profile_root = (profile_base / host).resolve()
        if profile_root.parent == profile_base and profile_root != profile_base and profile_root.exists():
            import shutil

            shutil.rmtree(profile_root, ignore_errors=True)

    def clear(self, origin: str) -> None:
        self._path(origin).unlink(missing_ok=True)
        self._clear_profile(urlsplit(origin).hostname or "")

    def list_statuses(self) -> list[dict]:
        result = []
        for path in self.root.glob("*.json"):
            try:
                data = self._read_json(path)
                origin = urlsplit(str(data.get("origin", "")))
                result.append(
                    {
                        "session_id": path.stem,
                        "host": origin.hostname or "",
                        "origin": data.get("origin", ""),
                        "validation_status": data.get("validation_status", "missing"),
                        "updated_at": data.get("updated_at"),
                        "last_success_at": data.get("last_success_at"),
                        "cookie_count": len(data.get("cookies", [])),
                    }
                )
            except (OSError, ValueError, TypeError):
                continue
        return result

    def origin_for_session(self, session_id: str) -> str | None:
        if not session_id or any(ch not in "0123456789abcdef" for ch in session_id.lower()):
            return None
        path = self.root / f"{session_id}.json"
        try:
            return str(self._read_json(path).get("origin", "")) or None
        except (OSError, ValueError, TypeError):
            return None

    def clear_expired(self) -> int:
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                data = self._read_json(path)
                cookies = [
                    c
                    for c in data.get("cookies", [])
                    if c.get("expires") in (None, -1, 0, "") or float(c["expires"]) > time.time()
                ]
                if not cookies:
                    path.unlink(missing_ok=True)
                    self._clear_profile(urlsplit(str(data.get("origin", ""))).hostname or "")
                    removed += 1
                elif len(cookies) != len(data.get("cookies", [])):
                    data["cookies"] = cookies
                    self._write_json(path, data)
            except (OSError, ValueError, TypeError):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def status(self, origin: str) -> dict:
        try:
            data = self._read_json(self._path(origin))
            return {
                **{k: data.get(k) for k in ("updated_at", "last_success_at", "validation_status", "browser_version")},
                "origin": origin,
                "session_id": self._key(origin),
            }
        except (OSError, ValueError):
            return {"validation_status": "missing", "origin": origin}
