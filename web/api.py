from __future__ import annotations

import dataclasses
import json
import re
import time
import uuid

from astrbot.api.web import PluginUploadFile, error_response, file_response, json_response, request

from ..services.management import ManagementService

_EXPORT_TTL_SECONDS = 24 * 60 * 60
_EXPORT_FILENAME = re.compile(r"rss-(?:export-[0-9a-f]{32}\.json|backup-[0-9a-f]{32}\.sqlite3)\Z")


def _plain(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class WebAPI:
    def __init__(self, runtime):
        self.runtime = runtime
        self.management = ManagementService(runtime)

    def _cleanup_expired_exports(self):
        """Remove only this API's expired download files; retry failures next time."""
        export_dir = self.runtime.data_dir / "exports"
        cutoff = time.time() - _EXPORT_TTL_SECONDS
        try:
            paths = export_dir.iterdir()
            for path in paths:
                if not _EXPORT_FILENAME.fullmatch(path.name):
                    continue
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    # A later export request retries cleanup if the file is busy or inaccessible.
                    continue
        except OSError:
            # Cleanup is best-effort and must not prevent a fresh download.
            return

    @staticmethod
    async def _body(request):
        value = await request.json(default={})
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 1_000_000:
            raise ValueError("request body exceeds 1 MiB")
        return value

    @staticmethod
    async def _auth(request):
        username = getattr(request, "username", None)
        if not isinstance(username, str) or not username.strip():
            raise PermissionError("authenticated AstrBot username is required")

    async def get(self, endpoint, request):
        try:
            await self._auth(request)
            query = dict(getattr(request, "query", {}) or {})
            if endpoint in {"data/backup", "data/export"}:
                self._cleanup_expired_exports()
                if endpoint == "data/backup":
                    path = self.runtime.data_dir / "exports" / f"rss-backup-{uuid.uuid4().hex}.sqlite3"
                    try:
                        await self.runtime.backup(path)
                    except BaseException:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise
                    return file_response(str(path), filename="rss-backup.sqlite3", content_type="application/x-sqlite3")
                path = self.runtime.data_dir / "exports" / f"rss-export-{uuid.uuid4().hex}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    data = await self.management.get(endpoint, query)
                    path.write_text(json.dumps(_plain(data), ensure_ascii=False, indent=2), "utf-8")
                except BaseException:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
                return file_response(str(path), filename="rss-export.json", content_type="application/json")
            result = await self.management.get(endpoint, query)
            return json_response(_plain(result))
        except PermissionError as exc:
            return error_response(str(exc), status_code=401)
        except KeyError as exc:
            return error_response(f"not found: {exc}", status_code=404)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response("internal management API error", status_code=500)

    async def post(self, endpoint, request):
        try:
            await self._auth(request)
            if endpoint in {"data/import", "data/restore"}:
                files = await request.files()
                upload = files.get("file")
                if not isinstance(upload, PluginUploadFile):
                    return error_response("missing upload file", status_code=400)
                declared_size = getattr(upload, "size", None)
                limit = 20_000_000 if endpoint == "data/import" else 100_000_000
                if declared_size is not None and (type(declared_size) is not int or not 0 < declared_size <= limit):
                    return error_response("upload exceeds allowed size", status_code=400)
                headers = getattr(request, "headers", {}) or {}
                content_length = headers.get("content-length") or headers.get("Content-Length")
                if declared_size is None:
                    try:
                        content_length = int(content_length)
                    except (TypeError, ValueError):
                        return error_response("upload size must be declared", status_code=400)
                    if not 0 < content_length <= limit + 1_000_000:
                        return error_response("upload exceeds allowed size", status_code=400)
                temp_dir = self.runtime.data_dir / "uploads"
                temp_dir.mkdir(parents=True, exist_ok=True)
                path = temp_dir / f"{uuid.uuid4().hex}.upload"
                try:
                    await upload.save(path)
                    actual_size = path.stat().st_size
                    if not 0 < actual_size <= limit:
                        return error_response("upload exceeds allowed size", status_code=400)
                    if declared_size is not None and actual_size != declared_size:
                        return error_response("upload size does not match declaration", status_code=400)
                    result = await self.management.upload(endpoint, path, upload.filename, request.username)
                finally:
                    path.unlink(missing_ok=True)
                return json_response(_plain(result))
            body = await self._body(request)
            body["_username"] = request.username
            result = await self.management.post(endpoint, body)
            return json_response(_plain(result))
        except PermissionError as exc:
            return error_response(str(exc), status_code=401)
        except KeyError as exc:
            return error_response(f"not found: {exc}", status_code=404)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response("internal management API error", status_code=500)

    def register(self, context):
        prefix = "/astrbot_plugin_rss/"
        get_routes = (
            "dashboard",
            "feeds",
            "feeds/<feed_id>",
            "lists",
            "lists/<list_id>",
            "targets",
            "deliveries",
            "deliveries/<delivery_id>",
            "data/status",
            "data/export",
            "data/backup",
        )
        post_routes = (
            "scheduler",
            "feeds/create",
            "feeds/<feed_id>/update",
            "feeds/<feed_id>/refresh",
            "feeds/<feed_id>/unsubscribe",
            "feeds/<feed_id>/preview",
            "feeds/<feed_id>/test",
            "lists/create",
            "lists/<list_id>/update",
            "lists/<list_id>/delete",
            "lists/<list_id>/members/add",
            "lists/<list_id>/members/remove",
            "lists/<list_id>/subscribe",
            "lists/<list_id>/unsubscribe",
            "lists/<list_id>/preview",
            "deliveries/<delivery_id>/retry",
            "deliveries/<delivery_id>/resolve",
            "data/import",
            "data/restore",
            "data/restore/confirm",
            "data/import/confirm",
            "data/cleanup",
            "sessions/<session_id>/clear",
            "sessions/<session_id>/refresh",
        )

        def make_handler(route, method):
            async def handler(**params):
                path_params = dict(getattr(request, "path_params", {}) or {})
                path_params.update(params)
                endpoint = self._resolve(route, path_params)
                if method == "GET":
                    return await self.get(endpoint, request)
                return await self.post(endpoint, request)

            return handler

        for route in get_routes:
            context.register_web_api(
                prefix + route, make_handler(route, "GET"), methods=["GET"], desc=f"RSS management: {route}"
            )
        for route in post_routes:
            context.register_web_api(
                prefix + route, make_handler(route, "POST"), methods=["POST"], desc=f"RSS management: {route}"
            )

    @staticmethod
    def _resolve(route, params):
        for key, value in params.items():
            route = route.replace("<" + key + ">", str(value))
        return route
