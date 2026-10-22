from __future__ import annotations

import asyncio
import email.utils
import math
import tempfile
import time
import xml.parsers.expat
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from curl_cffi import CurlOpt, ffi, lib
from curl_cffi.requests import AsyncSession

from ..core.feed_urls import build_feed_request, is_relative_feed_reference
from ..core.models import FetchResult
from ..core.security import UnsafeURLError, validate_public_url
from .compatibility import BrowserCompatibilityManager
from .cookies import CookieManager


class _LeasedResponse:
    def __init__(self, response, release) -> None:
        self._response = response
        self._release = release
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            quit_now = getattr(self._response, "quit_now", None)
            if quit_now is not None:
                quit_now.set()
            stream_task = getattr(self._response, "astream_task", None)
            if stream_task is not None and not stream_task.done():
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
                return
            close_task = asyncio.create_task(self._response.aclose())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await asyncio.gather(close_task, return_exceptions=True)
                raise
        finally:
            await self._release()


class FeedFetcher:
    """Reusable curl fetcher; every direct HTTP hop is DNS-validated and pinned."""

    def __init__(self, config, data_dir, session_factory=AsyncSession) -> None:
        self.config = config
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        session_root = (self.data_dir / config.session_storage_path).resolve()
        if session_root != self.data_dir.resolve() and self.data_dir.resolve() not in session_root.parents:
            raise ValueError("session_storage_path must stay inside plugin data directory")
        self.session_factory = session_factory
        self._sessions: OrderedDict[str, dict] = OrderedDict()
        self._session_condition = asyncio.Condition()
        self._session_limit = min(16, max(1, config.max_fetch_concurrency))
        self._feed_locks: dict[str, asyncio.Lock] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._browser_locks: dict[str, asyncio.Lock] = {}
        self._browser_last_attempt: dict[str, float] = {}
        self._global_semaphore = asyncio.Semaphore(max(1, config.max_fetch_concurrency))
        self._active_fetches: set[asyncio.Task] = set()
        self._browser_contexts: dict[int, object] = {}
        self._browser_semaphore = asyncio.Semaphore(max(1, config.max_browser_instances))
        self._closed = False
        self.cookies = CookieManager(session_root)
        self.compatibility = BrowserCompatibilityManager(config.cloakbrowser_version or None)

    def _session_key(
        self, validated, *, proxy_url: str | None = None, target_ip: str | None = None, impersonate: str | None = None
    ) -> tuple[str, dict]:
        selected_impersonate = impersonate or self.config.curl_impersonate_target
        resolve = validated.curl_resolve()
        options = {CurlOpt.RESOLVE: [resolve]} if resolve else {}
        if proxy_url:
            address = target_ip or validated.pinned_ips[0]
            authority_ip = f"[{address}]" if ":" in address else address
            connect_to = self._curl_slist([f"{validated.hostname}:{validated.port}:{authority_ip}:{validated.port}"])
            options = {
                CurlOpt.CONNECT_TO: connect_to,
                CurlOpt.HTTPPROXYTUNNEL: 1,
            }
        key = f"{validated.hostname}:{validated.port}|{sha256((proxy_url or '').encode()).hexdigest()}|{target_ip or resolve}|{selected_impersonate}"
        session_options = {
            "max_clients": 1,
            "headers": {"User-Agent": self.config.fetch_user_agent},
            "impersonate": selected_impersonate,
            "trust_env": False,
            "proxy": proxy_url,
            "curl_options": options,
        }
        return key, session_options

    async def _lease_session(
        self, validated, *, proxy_url: str | None = None, target_ip: str | None = None, impersonate: str | None = None
    ) -> tuple[str, dict, AsyncSession]:
        key, options = self._session_key(validated, proxy_url=proxy_url, target_ip=target_ip, impersonate=impersonate)
        async with self._session_condition:
            while True:
                if self._closed:
                    raise RuntimeError("FeedFetcher is closed")
                entry = self._sessions.get(key)
                if entry is not None:
                    if entry["active"]:
                        await self._session_condition.wait()
                        continue
                    entry["active"] += 1
                    self._sessions.move_to_end(key)
                    return key, entry, entry["session"]
                if len(self._sessions) < self._session_limit:
                    session = self.session_factory(**options)
                    entry = {"session": session, "active": 1}
                    self._sessions[key] = entry
                    return key, entry, session
                idle_key = next((candidate for candidate, item in self._sessions.items() if not item["active"]), None)
                if idle_key is not None:
                    idle = self._sessions.pop(idle_key)
                    try:
                        await self._close_session(idle["session"])
                    except asyncio.CancelledError:
                        self._session_condition.notify_all()
                        raise
                    except Exception:
                        pass
                    continue
                await self._session_condition.wait()

    async def _release_session(self, key: str, entry: dict) -> None:
        async with self._session_condition:
            current = self._sessions.get(key)
            if current is entry:
                entry["active"] = max(0, entry["active"] - 1)
                self._sessions.move_to_end(key)
            self._session_condition.notify_all()

    async def _request_with_lease(self, validated, *, proxy_url, target_ip, impersonate, **request_kwargs):
        key, entry, session = await self._lease_session(
            validated, proxy_url=proxy_url, target_ip=target_ip, impersonate=impersonate
        )
        released = False

        async def release() -> None:
            nonlocal released
            if not released:
                released = True
                await self._release_session(key, entry)

        try:
            response = await session.request("GET", validated.url, **request_kwargs)
        except BaseException:
            try:
                await self._invalidate_session(key, entry)
            finally:
                await release()
            raise
        return _LeasedResponse(response, release)

    async def _invalidate_session(self, key: str, entry: dict) -> None:
        async with self._session_condition:
            if self._sessions.get(key) is entry:
                self._sessions.pop(key)
        try:
            await self._close_session(entry["session"])
        finally:
            async with self._session_condition:
                self._session_condition.notify_all()

    @staticmethod
    async def _close_session(session) -> None:
        close_task = asyncio.create_task(session.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await asyncio.gather(close_task, return_exceptions=True)
            raise

    def _proxy_url(self) -> str | None:
        reference = self.config.proxy_profile.strip()
        if not reference:
            reference = self.config.proxy.strip()
        if not reference:
            return None
        if reference.startswith(("http://", "https://")):
            proxy_url = reference
        else:
            proxy_url = (getattr(self.config, "proxy_profiles", None) or {}).get(reference)
        if not proxy_url or urlsplit(proxy_url).scheme not in {"http", "https"} or not urlsplit(proxy_url).hostname:
            raise ValueError("proxy profile is not configured as an HTTP(S) proxy")
        return proxy_url

    @staticmethod
    def _curl_slist(values: list[str]):
        """Build a GC-managed option list kept alive by the session options."""
        head = ffi.NULL
        try:
            for value in values:
                appended = lib.curl_slist_append(head, value.encode("utf-8"))
                if appended == ffi.NULL:
                    raise MemoryError("libcurl could not allocate a curl_slist")
                head = appended
            if head == ffi.NULL:
                raise ValueError("curl_slist requires at least one entry")
            return ffi.gc(head, lib.curl_slist_free_all)
        except BaseException:
            if head != ffi.NULL:
                lib.curl_slist_free_all(head)
            raise

    async def fetch(self, feed, force: bool = False) -> FetchResult:
        if self._closed:
            raise RuntimeError("FeedFetcher is closed")
        task = asyncio.current_task()
        if task is not None:
            self._active_fetches.add(task)
        try:
            return await self._fetch_feed(feed, force)
        finally:
            if task is not None:
                self._active_fetches.discard(task)

    async def _fetch_feed(self, feed, force: bool = False) -> FetchResult:
        async with self._feed_locks.setdefault(feed.feed_id, asyncio.Lock()):
            async with self._global_semaphore:
                started = time.monotonic()
                try:
                    feed_request = build_feed_request(feed.url, self.config)
                except (TypeError, ValueError):
                    return self._error(feed, "unsafe_url", "feed URL failed public-address validation", started)
                request_feed = replace(feed, url=feed_request.request_url)
                try:
                    first = await validate_public_url(request_feed.url)
                except UnsafeURLError:
                    return self._error(
                        request_feed,
                        "unsafe_url",
                        "feed URL failed public-address validation",
                        started,
                        feed_request,
                    )
                headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*;q=0.5"}
                fingerprint_changed = (
                    is_relative_feed_reference(feed.url)
                    and feed.metadata.get("request_url_fingerprint") != feed_request.fingerprint
                )
                if self.config.enable_etag_cache and not force and not fingerprint_changed:
                    if feed.etag:
                        headers["If-None-Match"] = feed.etag
                    if feed.last_modified:
                        headers["If-Modified-Since"] = feed.last_modified
                try:
                    proxy_url = self._proxy_url()
                except ValueError:
                    return self._error(
                        request_feed,
                        "unsafe_proxy",
                        "configured proxy is not a supported public HTTP(S) proxy",
                        started,
                        feed_request,
                    )
                result = await self._request_chain(request_feed, first.url, headers, started, proxy_url, feed_request)
                if await self._should_browser_fallback(result):
                    hit = result.cookie_cache_hit
                    result = await self._browser_fetch(request_feed, result, started, feed_request)
                    result.browser_attempted = True
                    result.cookie_cache_hit = result.cookie_cache_hit or hit
                return result

    async def _request_chain(
        self,
        feed,
        url: str,
        headers: dict,
        started: float,
        proxy_url: str | None = None,
        feed_request=None,
    ) -> FetchResult:
        cookie_hit_any = False
        http_attempts = 0
        for attempt in range(self.config.request_retry_limit + 1):
            current, request_headers = url, dict(headers)
            try:
                for _ in range(6):
                    validated = await validate_public_url(current)
                    selected_impersonate = None
                    saved = []
                    compat = self.compatibility.inspect()
                    compatible_target = compat.get("impersonate_target")
                    configured_target = self.config.curl_impersonate_target
                    if compat.get("compatible") and (configured_target in {"chrome", compatible_target}):
                        selected_impersonate = compatible_target
                    if self.config.enable_cookie_persistence and selected_impersonate:
                        client_metadata = self.cookies.client_metadata_for_url(
                            current,
                            self.config.fetch_user_agent,
                            self.config.proxy_profile,
                            None,
                            selected_impersonate,
                            binary_version=compat.get("browser_version") or "",
                        )
                        target_major = "".join(ch for ch in str(compatible_target) if ch.isdigit())
                        browser_major = str(client_metadata.get("major_version", ""))
                        browser_ua = str(client_metadata.get("user_agent", ""))
                        if (
                            target_major
                            and browser_major != target_major
                            or browser_major
                            and f"Chrome/{browser_major}" not in browser_ua
                        ):
                            client_metadata = {}
                        bound_ua = client_metadata.get("user_agent") if isinstance(client_metadata, dict) else None
                        actual_version = client_metadata.get("actual_browser_version") if client_metadata else None
                        if isinstance(bound_ua, str) and bound_ua.startswith("Mozilla/") and actual_version:
                            saved = self.cookies.get_for_url(
                                current,
                                bound_ua,
                                self.config.proxy_profile,
                                actual_version,
                                selected_impersonate,
                            )
                    for header_name in ("sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"):
                        request_headers.pop(header_name, None)
                    if selected_impersonate and self.config.enable_cookie_persistence:
                        major = str(client_metadata.get("major_version", ""))
                        platform_name = str(client_metadata.get("platform", ""))
                        if major.isdigit() and platform_name in {"Windows", "macOS", "Linux", "Android", "Chrome OS"}:
                            if isinstance(client_metadata.get("user_agent"), str):
                                request_headers["User-Agent"] = client_metadata["user_agent"]
                            request_headers["sec-ch-ua"] = f'"Chromium";v="{major}", "Google Chrome";v="{major}"'
                            request_headers["sec-ch-ua-platform"] = f'"{platform_name}"'
                            request_headers["sec-ch-ua-mobile"] = "?1" if client_metadata.get("mobile") else "?0"
                    cookie_hit = bool(saved)
                    cookie_hit_any = cookie_hit_any or cookie_hit
                    request_headers.pop("Cookie", None)
                    if saved:
                        request_headers["Cookie"] = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in saved)
                    host_lock = self._domain_locks.setdefault(validated.hostname, asyncio.Lock())
                    async with host_lock:
                        previous = self._last_request.get(validated.hostname)
                        delay = 1.0 - (time.monotonic() - previous) if previous is not None else 0.0
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._last_request[validated.hostname] = time.monotonic()
                        http_attempts += 1
                        response = await self._request_with_lease(
                            validated,
                            proxy_url=proxy_url,
                            target_ip=validated.pinned_ips[0] if proxy_url else None,
                            impersonate=selected_impersonate,
                            headers=request_headers,
                            allow_redirects=False,
                            timeout=self.config.request_timeout,
                            stream=True,
                            max_redirects=0,
                            discard_cookies=True,
                        )
                    try:
                        status = int(response.status_code)
                        rh = {str(k).lower(): str(v) for k, v in response.headers.items()}
                    except BaseException:
                        await self._close_response(response)
                        raise
                    if status == 429:
                        await self._close_response(response)
                        retry_after_present = "retry-after" in rh
                        retry_after_raw = rh.get("retry-after")
                        delay, retry_after_valid = self._parse_retry_after(retry_after_raw)
                        retry_after_at = time.time() + delay
                        if not math.isfinite(retry_after_at):
                            delay = 60.0
                            retry_after_valid = False
                            retry_after_at = time.time() + delay
                        details = [f"本轮请求尝试={http_attempts}"]
                        if not retry_after_present:
                            details.append("Retry-After 未提供，使用默认60秒")
                        else:
                            safe_retry_after = self._safe_429_header("retry-after", retry_after_raw)
                            if safe_retry_after:
                                if retry_after_valid:
                                    details.append(f"Retry-After={safe_retry_after}")
                                else:
                                    details.append(f"Retry-After={safe_retry_after}（无效，使用默认60秒）")
                            else:
                                details.append("Retry-After 无有效值，使用默认60秒")
                        for name in (
                            "discourse-rate-limit-error-code",
                            "cf-ray",
                            "server",
                            "cf-cache-status",
                            "age",
                            "content-type",
                            "cf-mitigated",
                        ):
                            safe_value = self._safe_429_header(name, rh.get(name))
                            if safe_value:
                                detail = f"{name}={safe_value}"
                                if (
                                    len(
                                        f"HTTP 429；请求受限，至少等待 {delay:g} 秒后重试；"
                                        + "；".join(details + [detail])
                                    )
                                    <= 768
                                ):
                                    details.append(detail)
                        return self._result(
                            feed,
                            current,
                            status,
                            rh,
                            error_type="http_429",
                            error_message=(f"HTTP 429；请求受限，至少等待 {delay:g} 秒后重试；" + "；".join(details)),
                            started=started,
                            cookie_cache_hit=cookie_hit_any,
                            retry_after_at=retry_after_at,
                            feed_request=feed_request,
                        )
                    if status in {301, 302, 303, 307, 308}:
                        location = rh.get("location")
                        await self._close_response(response)
                        if not location:
                            return self._result(
                                feed,
                                current,
                                status,
                                rh,
                                error_type="redirect_missing_location",
                                error_message="redirect omitted Location",
                                started=started,
                                feed_request=feed_request,
                            )
                        redirected = urljoin(current, location)
                        if feed_request is not None:
                            redirected = feed_request.redirect_url(redirected)
                        new_parts = urlsplit(redirected)
                        # Validate before next request. Drop credentials and validators across origins.
                        old_parts = urlsplit(current)
                        if (old_parts.scheme, old_parts.hostname, old_parts.port) != (
                            new_parts.scheme,
                            new_parts.hostname,
                            new_parts.port,
                        ):
                            for key in ("Cookie", "If-None-Match", "If-Modified-Since", "Authorization"):
                                request_headers.pop(key, None)
                        current = redirected
                        continue
                    if status == 304:
                        await self._close_response(response)
                        if saved and self.config.enable_cookie_persistence:
                            self.cookies.mark_validated_for_url(current)
                        return FetchResult(
                            feed.feed_id,
                            feed_request.public_url if feed_request else feed.url,
                            feed_request.redact_url(current) if feed_request else current,
                            304,
                            rh.get("content-type", ""),
                            b"",
                            rh.get("etag", feed.etag),
                            rh.get("last-modified", feed.last_modified),
                            fetch_method="curl",
                            elapsed_ms=self._elapsed(started),
                            cookie_cache_hit=cookie_hit_any,
                            request_url_fingerprint=feed_request.fingerprint if feed_request else None,
                        )
                    body = bytearray()
                    too_large = False
                    try:
                        async for chunk in response.aiter_content():
                            body.extend(chunk)
                            if len(body) > self.config.max_feed_response_bytes:
                                too_large = True
                                break
                    finally:
                        await self._close_response(response)
                    if too_large:
                        return self._result(
                            feed,
                            current,
                            status,
                            rh,
                            error_type="response_too_large",
                            error_message="response exceeded configured byte limit",
                            started=started,
                            cookie_cache_hit=cookie_hit_any,
                            feed_request=feed_request,
                        )
                    raw = bytes(body)
                    if status == 200 and not self._looks_like_feed_xml(raw, rh.get("content-type", "")):
                        return self._result(
                            feed,
                            current,
                            status,
                            rh,
                            raw,
                            "non_xml_response",
                            "response root is not valid RSS, Atom, or RDF",
                            started,
                            cookie_cache_hit=cookie_hit_any,
                            feed_request=feed_request,
                        )
                    if status in {401, 403} and request_headers.get("Cookie") and self.config.enable_cookie_persistence:
                        self.cookies.mark_invalid_for_url(current)
                    elif status == 200 and request_headers.get("Cookie") and self.config.enable_cookie_persistence:
                        self.cookies.mark_validated_for_url(current)
                    if status in {500, 502, 503, 504} and attempt < self.config.request_retry_limit:
                        await asyncio.sleep(self._retry_delay(rh.get("retry-after"), attempt))
                        break
                    return self._result(
                        feed,
                        current,
                        status,
                        rh,
                        raw,
                        None if status == 200 else f"http_{status}",
                        None if status == 200 else f"HTTP {status}",
                        started,
                        cookie_cache_hit=cookie_hit,
                        feed_request=feed_request,
                    )
                else:
                    return self._error(feed, "redirect_limit", "too many redirects", started, feed_request)
            except UnsafeURLError:
                return self._error(
                    feed, "unsafe_redirect", "redirect failed public-address validation", started, feed_request
                )
            except Exception as exc:
                if attempt < self.config.request_retry_limit:
                    await asyncio.sleep(min(self.config.retry_backoff_base * (2**attempt), 30))
                    continue
                return self._error(
                    feed, type(exc).__name__, "upstream request failed; details omitted", started, feed_request
                )
        return self._error(feed, "retry_exhausted", "retry limit exhausted", started, feed_request)

    @staticmethod
    def _retry_delay(value: str | None, attempt: int) -> float:
        if value:
            try:
                return min(120.0, max(0.0, float(value)))
            except ValueError:
                try:
                    date = email.utils.parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=UTC)
                    return min(120.0, max(0.0, (date - datetime.now(UTC)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(30.0, 2.0 * (2**attempt))

    @staticmethod
    def _parse_retry_after(value: str | None) -> tuple[float, bool]:
        if value is not None:
            try:
                delay = float(value)
                return (delay, True) if math.isfinite(delay) and delay >= 0 else (60.0, False)
            except ValueError:
                try:
                    date = email.utils.parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=UTC)
                    delay = date.timestamp() - time.time()
                    return (max(0.0, delay), True) if math.isfinite(delay) else (60.0, False)
                except (TypeError, ValueError, OverflowError):
                    pass
        return 60.0, False

    @staticmethod
    def _safe_429_header(name: str, value: str | None) -> str:
        if not value:
            return ""
        for separator in ("\r", "\n", "\x85", "\u2028", "\u2029"):
            value = value.split(separator, 1)[0]
        allowed = {
            "retry-after": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ,:+./-"),
            "server": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._,/+-"),
            "discourse-rate-limit-error-code": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"),
            "cf-ray": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"),
            "cf-cache-status": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"),
            "age": set("0123456789"),
            "content-type": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ;=._/-+"),
            "cf-mitigated": set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"),
        }.get(name)
        if allowed is None:
            return ""
        chars = []
        for char in value:
            codepoint = ord(char)
            if 32 <= codepoint <= 126 and char in allowed:
                chars.append(char)
            elif codepoint < 32 or codepoint > 126:
                chars.append("?")
            else:
                chars.append("?")
        safe = "".join(chars).strip()
        if len(safe) > 96:
            safe = safe[:93] + "..."
        return safe

    @staticmethod
    async def _close_response(response) -> None:
        try:
            await response.aclose()
        except Exception:
            return

    @staticmethod
    def _looks_like_feed_xml(body: bytes, content_type: str) -> bool:
        if not body or not ("xml" in content_type.lower() or body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")):
            return False
        roots: list[str] = []
        parser = xml.parsers.expat.ParserCreate(namespace_separator="}")
        parser.StartElementHandler = lambda name, _attrs: (
            roots.append(name.rsplit("}", 1)[-1].lower()) if not roots else None
        )

        def reject(*_args):
            raise ValueError("DTD/entity declarations are disabled")

        parser.StartDoctypeDeclHandler = reject
        parser.EntityDeclHandler = reject
        parser.ExternalEntityRefHandler = lambda *_args: 0
        try:
            parser.Parse(body, True)
        except Exception:
            return False
        return bool(roots and roots[0] in {"rss", "feed", "rdf"})

    async def _should_browser_fallback(self, result: FetchResult) -> bool:
        if not self.config.enable_cloakbrowser or result.status_code not in {403, 503}:
            return False
        host = (urlsplit(result.final_url or result.requested_url).hostname or "").lower()
        allowed = tuple(d.lower().lstrip(".") for d in self.config.browser_allowed_domains)
        if not any(host == d or host.endswith("." + d) for d in allowed):
            return False
        body = result.raw_xml[:100_000].lower()
        return any(
            mark in body for mark in (b"cf-chl-", b"cf_clearance", b"just a moment", b"challenge-platform", b"captcha")
        )

    async def _browser_fetch(self, feed, prior: FetchResult, started: float, feed_request=None) -> FetchResult:
        safe_feed = replace(feed, url=feed_request.public_url) if feed_request else feed
        host = (urlsplit(prior.final_url or safe_feed.url).hostname or "").lower()
        async with self._browser_locks.setdefault(host, asyncio.Lock()):
            cooldown = self.config.browser_retry_cooldown
            if host in self._browser_last_attempt and time.monotonic() - self._browser_last_attempt[host] < cooldown:
                return self._error(
                    safe_feed, "browser_cooldown", "browser fallback cooling down for this host", started, feed_request
                )
            self._browser_last_attempt[host] = time.monotonic()
            try:
                async with asyncio.timeout(self.config.browser_timeout):
                    async with self._browser_semaphore:
                        return await self._browser_fetch_locked(safe_feed, prior, started, feed_request)
            except TimeoutError:
                return self._error(
                    safe_feed,
                    "browser_timeout",
                    "browser fallback exceeded its total time limit",
                    started,
                    feed_request,
                )

    async def _browser_fetch_locked(self, feed, prior: FetchResult, started: float, feed_request=None) -> FetchResult:
        temporary_profile = None
        try:
            from cloakbrowser import binary_info, launch_persistent_context_async

            info = binary_info(browser_version=self.config.cloakbrowser_version or None)
            if not info.get("installed"):
                return self._error(
                    feed, "browser_unavailable", "Chromium binary not installed; download was not attempted", started
                )
            version = info.get("installed_version") or info.get("version")
            compatibility = BrowserCompatibilityManager(version).inspect()
            if not compatibility["compatible"] or self.config.curl_impersonate_target not in {
                "chrome",
                compatibility["impersonate_target"],
            }:
                return self._error(
                    feed,
                    "browser_incompatible",
                    "installed Chromium version has no supported curl_cffi target",
                    started,
                )
            if self.config.proxy_profile or self.config.proxy:
                return self._error(
                    feed, "browser_proxy_unsupported", "browser proxy DNS pinning has not been verified", started
                )
            url = prior.final_url or feed.url
            if feed_request is not None:
                url = feed_request.redirect_url(url)
            validated = await validate_public_url(url)
            profile_root = (self.data_dir / self.config.session_storage_path).resolve()
            if self.config.enable_cookie_persistence:
                profile = profile_root / validated.hostname
                profile.mkdir(parents=True, exist_ok=True)
            else:
                temporary_profile = tempfile.TemporaryDirectory(prefix="astrbot-rss-browser-")
                profile = Path(temporary_profile.name)
            map_rule = f"--host-resolver-rules=MAP {validated.hostname} {validated.pinned_ips[0]},MAP * ~NOTFOUND"
            context = await launch_persistent_context_async(
                profile,
                headless=True,
                browser_version=version,
                proxy=None,
                args=[map_rule],
                service_workers="block",
            )
            self._browser_contexts[id(context)] = context
        except Exception as exc:
            return self._error(
                feed, "browser_unavailable", f"browser initialization failed: {type(exc).__name__}", started
            )

        try:
            browser = getattr(context, "browser", None)
            actual_version = getattr(browser, "version", None) if browser is not None else None
            if not isinstance(actual_version, str) or not actual_version:
                return self._error(
                    feed, "browser_incompatible", "browser context did not expose its actual version", started
                )
            expected_major = str(version).split(".", 1)[0]
            actual_major = actual_version.split(".", 1)[0]
            if actual_major != expected_major:
                return self._error(
                    feed, "browser_incompatible", "actual Chromium version differs from selected binary", started
                )
            binary_version = version
            version = actual_version

            async def guard(route):
                try:
                    requested = await validate_public_url(route.request.url)
                    if (
                        requested.hostname != validated.hostname
                        or urlsplit(requested.url).scheme != urlsplit(validated.url).scheme
                    ):
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    await route.abort()

            await context.route("**/*", guard)
            if not hasattr(context, "route_web_socket"):
                return self._error(
                    feed, "browser_safety_unavailable", "installed Playwright cannot block WebSocket routes", started
                )

            async def block_websocket(route):
                await route.close(code=1008, reason="RSS fallback only permits pinned HTTP(S) feed requests")

            await context.route_web_socket("**/*", block_websocket)
            page = context.pages[0] if context.pages else await context.new_page()
            browser_metadata = {}
            try:
                browser_metadata = await page.evaluate(
                    "() => { const p=navigator.userAgentData?.platform || navigator.platform || ''; const platform=/win/i.test(p)?'Windows':/mac/i.test(p)?'macOS':/android/i.test(p)?'Android':/cros/i.test(p)?'Chrome OS':/linux/i.test(p)?'Linux':p; return {platform, mobile:!!navigator.userAgentData?.mobile, major_version:(navigator.userAgent.match(/(?:Chrome|Chromium)\\/(\\d+)/)||[])[1]||'', user_agent:navigator.userAgent}; }"
                )
            except Exception:
                browser_metadata = {}
            if not isinstance(browser_metadata, dict):
                browser_metadata = {}
            browser_metadata.setdefault("major_version", actual_major)
            browser_ua = browser_metadata.get("user_agent")
            if not isinstance(browser_ua, str) or f"Chrome/{actual_major}" not in browser_ua:
                return self._error(
                    feed,
                    "browser_incompatible",
                    "browser User-Agent could not be matched to its actual version",
                    started,
                )
            browser_metadata["actual_browser_version"] = actual_version
            browser_metadata["binary_version"] = binary_version

            # Playwright response.body() buffers the whole response before returning.
            # Only read responses with a small, explicit wire length and no content
            # encoding; all other candidates are retried through the bounded curl
            # streaming path below.
            captured: list[tuple[dict, bytes]] = []
            capture_tasks: set[asyncio.Task] = set()
            candidate_count = 0
            reserved_bytes = 0
            accepted_types = {
                "application/atom+xml",
                "application/rss+xml",
                "application/rdf+xml",
                "application/xml",
                "text/xml",
            }

            async def capture_response(response):
                nonlocal candidate_count, reserved_bytes
                try:
                    response_headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
                    media_type = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media_type not in accepted_types or int(response.status) != 200:
                        return
                    response_url = urlsplit(str(response.url))
                    if (
                        response_url.hostname != validated.hostname
                        or response_url.scheme != urlsplit(validated.url).scheme
                    ):
                        return
                    if response_headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                        return
                    declared = int(response_headers.get("content-length", ""))
                    if declared <= 0 or declared > self.config.max_feed_response_bytes:
                        return
                    if candidate_count >= 8 or reserved_bytes + declared > self.config.max_feed_response_bytes:
                        return
                    candidate_count += 1
                    reserved_bytes += declared
                    body = await response.body()
                    if len(body) > declared or len(body) > self.config.max_feed_response_bytes:
                        return
                    if not self._looks_like_feed_xml(body, media_type):
                        return
                    captured.append((response_headers, body))
                except (Exception, asyncio.CancelledError):
                    return

            def on_response(response):
                task = asyncio.create_task(capture_response(response))
                capture_tasks.add(task)
                task.add_done_callback(capture_tasks.discard)

            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=int(self.config.browser_timeout * 1000))
            if capture_tasks:
                await asyncio.gather(*list(capture_tasks), return_exceptions=True)
            final_url = str(getattr(page, "url", "") or url)
            if captured:
                if self.config.enable_cookie_persistence:
                    self.cookies.save(
                        f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
                        await context.cookies(),
                        browser_metadata.get("user_agent", self.config.fetch_user_agent),
                        self.config.proxy_profile,
                        version,
                        compatibility["impersonate_target"],
                        browser_metadata=browser_metadata,
                    )
                response_headers, response_body = captured[0]
                return FetchResult(
                    feed.feed_id,
                    feed_request.public_url if feed_request else feed.url,
                    feed_request.redact_url(final_url) if feed_request else final_url,
                    200,
                    response_headers.get("content-type", ""),
                    response_body,
                    response_headers.get("etag"),
                    response_headers.get("last-modified"),
                    fetch_method="browser",
                    elapsed_ms=self._elapsed(started),
                    cookie_cache_hit=prior.cookie_cache_hit,
                    browser_attempted=True,
                    request_url_fingerprint=feed_request.fingerprint if feed_request else None,
                )
            # Only inspect the rendered page after a valid, bounded feed response
            # has been ruled out. Legitimate article text may contain challenge words.
            page_url = final_url.lower()
            try:
                page_text = await page.evaluate(
                    "() => ((document.title || '') + ' ' + (document.body?.innerText || '')).slice(0, 20000)"
                )
            except Exception:
                page_text = ""
            challenge_markers = ("captcha", "verify you are human", "cf-chl-", "challenge-platform", "just a moment")
            if any(marker in page_url or marker in str(page_text).lower() for marker in challenge_markers):
                return self._error(
                    feed, "interaction_required", "browser reached an interactive verification page", started
                )
            if self.config.enable_cookie_persistence:
                self.cookies.save(
                    f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
                    await context.cookies(),
                    browser_metadata.get("user_agent", self.config.fetch_user_agent),
                    self.config.proxy_profile,
                    version,
                    compatibility["impersonate_target"],
                    browser_metadata=browser_metadata,
                )
            refetched = await self._request_chain(
                feed,
                url,
                {"Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*;q=0.5"},
                started,
                None,
                feed_request,
            )
            if refetched.status_code == 200 and refetched.error_type is None:
                return refetched
            if refetched.status_code == 429:
                return refetched
            body = refetched.raw_xml[:100_000].lower()
            if any(mark in body for mark in (b"captcha", b"verify you are human", b"cf-chl-", b"challenge-platform")):
                return self._error(
                    feed,
                    "interaction_required",
                    "site still requires interactive verification; fallback stopped",
                    started,
                )
            return self._error(
                feed, "browser_no_feed_xml", "bounded curl refetch did not return valid feed XML", started
            )
        except Exception as exc:
            return self._error(feed, "browser_error", type(exc).__name__, started)
        finally:
            tasks = [task for task in locals().get("capture_tasks", ()) if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(context.close(), timeout=2)
            except Exception:
                pass
            self._browser_contexts.pop(id(context), None)
            if temporary_profile is not None:
                temporary_profile.cleanup()

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _result(
        feed,
        final_url,
        status,
        headers,
        body=b"",
        error_type=None,
        error_message=None,
        started=0.0,
        cookie_cache_hit=False,
        retry_after_at=None,
        feed_request=None,
    ):
        return FetchResult(
            feed.feed_id,
            feed_request.public_url if feed_request else feed.url,
            feed_request.redact_url(final_url) if feed_request else final_url,
            status,
            headers.get("content-type", ""),
            body,
            headers.get("etag"),
            headers.get("last-modified"),
            fetch_method="curl",
            elapsed_ms=FeedFetcher._elapsed(started) if started else 0,
            error_type=error_type,
            error_message=error_message,
            cookie_cache_hit=cookie_cache_hit,
            retry_after_at=retry_after_at,
            request_url_fingerprint=feed_request.fingerprint if feed_request else None,
        )

    @staticmethod
    def _error(feed, kind: str, message: str, started: float, feed_request=None) -> FetchResult:
        return FetchResult(
            feed.feed_id,
            feed_request.public_url if feed_request else feed.url,
            feed_request.public_url if feed_request else feed.url,
            error_type=kind,
            error_message=message,
            elapsed_ms=FeedFetcher._elapsed(started),
            request_url_fingerprint=feed_request.fingerprint if feed_request else None,
        )

    async def close(self) -> None:
        async with self._session_condition:
            self._closed = True
            self._session_condition.notify_all()
        current = asyncio.current_task()
        active = [task for task in self._active_fetches if task is not current and not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for context in list(self._browser_contexts.values()):
            try:
                await asyncio.wait_for(context.close(), timeout=2)
            except Exception:
                pass
        self._browser_contexts.clear()
        async with self._session_condition:
            while any(entry["active"] for entry in self._sessions.values()):
                await self._session_condition.wait()
            sessions = [entry["session"] for entry in self._sessions.values()]
            self._sessions.clear()
            self._session_condition.notify_all()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
