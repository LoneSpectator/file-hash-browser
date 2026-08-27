from __future__ import annotations

import json
import re
import socket
import sys
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .algorithms import AlgorithmRegistry, PluginError, load_registry
from .config import AppConfig
from .jobs import JobManager, JobRequestError
from .paths import PathAccessError, PathAuthority
from .store import Store, StoreError


NODE_CHILDREN_PATTERN = re.compile(r"^/api/v1/nodes/([A-Za-z0-9_-]{1,128})/children$")
JOB_PATTERN = re.compile(r"^/api/v1/jobs/([a-f0-9]{32})$")
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_REQUEST_BODY = 64 * 1024


class StartupError(RuntimeError):
    pass


@dataclass(slots=True)
class Services:
    config: AppConfig
    registry: AlgorithmRegistry
    store: Store
    authority: PathAuthority
    jobs: JobManager
    static_assets: dict[str, tuple[str, bytes]]


class AppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], services: Services):
        self.services = services
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AppHTTPServer
    server_version = "FileHashBrowser"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.services.config.server.request_timeout_seconds)

    def log_message(self, format_string: str, *args: object) -> None:
        # Request URLs only contain opaque IDs. Never log exception/path text here.
        sys.stderr.write(
            f"{self.address_string()} [{self.log_date_time_string()}] "
            f"{format_string % args}\n"
        )

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:
        self.close_connection = True
        self._send_error_json(405, "method_not_allowed", "不支持此请求方法")

    def do_PUT(self) -> None:
        self.close_connection = True
        self._send_error_json(405, "method_not_allowed", "不支持此请求方法")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._valid_host():
                self.close_connection = True
                self._send_error_json(400, "invalid_host", "Host 不在服务端允许列表中")
                return
            if method in {"POST", "DELETE"} and not self._same_origin():
                # Reject before consuming a potentially attacker-controlled body.
                # Closing the HTTP/1.1 connection prevents unread bytes from being
                # interpreted as a second request by BaseHTTPRequestHandler.
                self.close_connection = True
                self._send_error_json(403, "cross_origin_denied", "不允许跨站创建任务")
                return
            parsed = urlsplit(self.path)
            if method in {"GET", "HEAD"}:
                self._handle_get(parsed.path, parsed.query, head_only=method == "HEAD")
            elif method == "POST":
                self._handle_post(parsed.path)
            elif method == "DELETE":
                self._handle_delete(parsed.path)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except PathAccessError as exc:
            self._send_error_json(404, exc.code, str(exc))
        except JobRequestError as exc:
            self._send_error_json(exc.http_status, exc.code, str(exc))
        except StoreError:
            self._send_error_json(503, "storage_unavailable", "数据暂时无法读取")
        except ValueError:
            self._send_error_json(400, "invalid_request", "请求格式无效")
        except Exception as exc:
            sys.stderr.write(f"Request failed safely ({type(exc).__name__}).\n")
            self._send_error_json(500, "internal_error", "服务端暂时无法完成请求")

    def _handle_get(self, path: str, query: str, *, head_only: bool) -> None:
        if path in self.server.services.static_assets:
            content_type, body = self.server.services.static_assets[path]
            cache = "no-cache" if path in {"/", "/index.html"} else "public, max-age=3600"
            self._send_bytes(
                200,
                body,
                content_type,
                head_only=head_only,
                extra_headers={"Cache-Control": cache},
            )
            return
        if head_only:
            self._send_error_json(405, "method_not_allowed", "此接口不支持 HEAD 请求")
            return
        if path == "/api/v1/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/api/v1/bootstrap":
            config = self.server.services.config
            self._send_json(
                200,
                {
                    "app": {
                        "title": config.title,
                        "nameVisibility": (
                            "full" if config.privacy.show_full_filename else "masked"
                        ),
                        "parallelism": {
                            "mode": config.parallelism_mode,
                            "effective": config.effective_parallel_tasks,
                        },
                        "defaultPageSize": config.browse.default_page_size,
                        "maxPageSize": config.browse.max_page_size,
                    },
                    "algorithms": self.server.services.registry.public_algorithms(),
                    "roots": self.server.services.authority.roots_public(),
                    "activeJobs": self.server.services.store.active_job_count(),
                },
            )
            return
        match = NODE_CHILDREN_PATTERN.fullmatch(path)
        if match:
            parameters = parse_qs(query, keep_blank_values=True)
            if set(parameters) - {"offset", "limit"}:
                raise ValueError("unknown query parameter")
            offset = self._query_integer(parameters, "offset", 0, 0, 2_147_483_647)
            config = self.server.services.config
            limit = self._query_integer(
                parameters,
                "limit",
                config.browse.default_page_size,
                1,
                config.browse.max_page_size,
            )
            node_id = match.group(1)
            parent = self.server.services.authority.resolve_tokens([node_id])[0]
            page = self.server.services.authority.list_children(node_id, offset, limit)
            self.server.services.store.prune_directory(
                parent,
                page.present_files,
                scanned_at=page.scanned_at,
            )
            hashes = self.server.services.store.hashes_for(
                item.ref for item in page.items
            )
            items: list[dict[str, object]] = []
            for item in page.items:
                public = item.public_dict()
                public["hashes"] = hashes.get(
                    (item.ref.root_id, item.ref.relative_path), []
                )
                items.append(public)
            self._send_json(
                200,
                {
                    "items": items,
                    "offset": page.offset,
                    "total": page.total,
                    "nextOffset": page.next_offset,
                },
            )
            return
        if path == "/api/v1/jobs":
            parameters = parse_qs(query, keep_blank_values=True)
            if set(parameters) - {"limit"}:
                raise ValueError("unknown query parameter")
            limit = self._query_integer(parameters, "limit", 50, 1, 100)
            jobs = [item.public_dict() for item in self.server.services.store.list_jobs(limit)]
            self._send_json(200, {"jobs": jobs})
            return
        match = JOB_PATTERN.fullmatch(path)
        if match:
            job = self.server.services.store.get_job(match.group(1))
            if job is None:
                self._send_error_json(404, "job_not_found", "任务不存在")
                return
            self._send_json(200, {"job": job.public_dict()})
            return
        self._send_error_json(404, "not_found", "请求的资源不存在")

    def _handle_post(self, path: str) -> None:
        cancel_match = re.fullmatch(r"/api/v1/jobs/([a-f0-9]{32})/cancel", path)
        if cancel_match:
            self._require_empty_body()
            cancelled = self.server.services.jobs.cancel(cancel_match.group(1))
            if not cancelled:
                self._send_error_json(404, "job_not_found", "任务不存在或已经结束")
                return
            self._send_json(202, {"cancelled": True})
            return
        body = self._read_json_body()
        if path == "/api/v1/jobs":
            self._require_keys(body, {"items", "algorithmIds", "strategy"})
            raw_items = body.get("items")
            if not isinstance(raw_items, list):
                raise ValueError("items")
            entry_ids: list[str] = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    raise ValueError("item")
                self._require_keys(raw_item, {"entryId", "recursive"})
                entry_id = raw_item.get("entryId")
                if not isinstance(entry_id, str):
                    raise ValueError("entryId")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", entry_id):
                    raise ValueError("entryId")
                recursive = raw_item.get("recursive", True)
                if not isinstance(recursive, bool):
                    raise ValueError("recursive")
                entry_ids.append(entry_id)
            raw_algorithms = body.get("algorithmIds")
            if not isinstance(raw_algorithms, list) or not all(
                isinstance(item, str) for item in raw_algorithms
            ):
                raise ValueError("algorithmIds")
            strategy = body.get("strategy", "missing-only")
            if not isinstance(strategy, str):
                raise ValueError("strategy")
            idempotency_key = self.headers.get("Idempotency-Key")
            if idempotency_key is not None and not IDEMPOTENCY_PATTERN.fullmatch(
                idempotency_key
            ):
                raise JobRequestError("invalid_idempotency_key", "幂等键格式无效")
            job, created = self.server.services.jobs.submit(
                entry_ids=entry_ids,
                algorithm_ids=raw_algorithms,
                strategy=strategy,
                idempotency_key=idempotency_key,
            )
            self._send_json(
                202 if created else 200,
                {"job": job.public_dict(), "created": created},
                extra_headers={"Location": f"/api/v1/jobs/{job.id}"},
            )
            return
        if path == "/api/v1/hashes/query":
            self._require_keys(body, {"entryIds"})
            entry_ids = body.get("entryIds")
            if (
                not isinstance(entry_ids, list)
                or len(entry_ids) > 500
                or not all(
                    isinstance(item, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item)
                    for item in entry_ids
                )
            ):
                raise ValueError("entryIds")
            resolved = [
                (entry_id, self.server.services.authority.resolve_tokens([entry_id])[0])
                for entry_id in entry_ids
            ]
            refs = tuple(ref for _entry_id, ref in resolved)
            hashes = self.server.services.store.hashes_for(refs)
            result: dict[str, object] = {}
            for entry_id, ref in resolved:
                result[entry_id] = hashes.get((ref.root_id, ref.relative_path), [])
            self._send_json(200, {"hashes": result})
            return
        self._send_error_json(404, "not_found", "请求的资源不存在")

    def _handle_delete(self, path: str) -> None:
        self._require_empty_body()
        if path == "/api/v1/jobs":
            removed = self.server.services.jobs.clear_all()
            self._send_json(200, {"removed": removed})
            return
        match = JOB_PATTERN.fullmatch(path)
        if match:
            removed = self.server.services.jobs.cancel(match.group(1))
            if not removed:
                self._send_error_json(404, "job_not_found", "任务不存在或已经结束")
                return
            self._send_json(200, {"removed": True})
            return
        self._send_error_json(404, "not_found", "请求的资源不存在")

    def _require_empty_body(self) -> None:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise JobRequestError("unexpected_body", "此请求不接受请求体", 400)
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise ValueError("Content-Length") from exc
        if length != 0:
            self.close_connection = True
            raise JobRequestError("unexpected_body", "此请求不接受请求体", 400)

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise JobRequestError("unsupported_transfer_encoding", "不支持分块请求体", 400)
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise JobRequestError("unsupported_media_type", "请求必须使用 application/json", 415)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise JobRequestError("length_required", "请求缺少 Content-Length", 411)
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BODY:
            raise JobRequestError("request_too_large", "请求体超过大小限制", 413)
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON object")
        return value

    @staticmethod
    def _require_keys(value: dict[str, Any], allowed: set[str]) -> None:
        if set(value) - allowed:
            raise ValueError("unknown field")

    @staticmethod
    def _query_integer(
        parameters: dict[str, list[str]],
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        values = parameters.get(key)
        if values is None:
            return default
        if len(values) != 1:
            raise ValueError(key)
        value = int(values[0])
        if not minimum <= value <= maximum:
            raise ValueError(key)
        return value

    def _valid_host(self) -> bool:
        allowed = self.server.services.config.server.allowed_hosts
        if "*" in allowed:
            return True
        raw_host = self.headers.get("Host", "").strip().lower()
        if not raw_host:
            return False
        if raw_host in allowed:
            return True
        try:
            hostname = urlsplit(f"//{raw_host}").hostname
        except ValueError:
            return False
        normalized_allowed = {item.strip("[]") for item in allowed}
        return bool(hostname and hostname.lower().strip("[]") in normalized_allowed)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        host = self.headers.get("Host", "").lower()
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host

    def _send_json(
        self,
        status: int,
        payload: object,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers=headers)

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


def _load_static_assets() -> dict[str, tuple[str, bytes]]:
    directory = Path(__file__).with_name("static")
    definitions = {
        "/index.html": ("text/html; charset=utf-8", "index.html"),
        "/assets/styles.css": ("text/css; charset=utf-8", "styles.css"),
        "/assets/app.js": ("text/javascript; charset=utf-8", "app.js"),
        "/favicon.svg": ("image/svg+xml", "favicon.svg"),
    }
    assets: dict[str, tuple[str, bytes]] = {}
    try:
        for route, (content_type, filename) in definitions.items():
            assets[route] = (content_type, (directory / filename).read_bytes())
    except OSError as exc:
        raise StartupError("web assets are missing from the installation") from exc
    assets["/"] = assets["/index.html"]
    return assets


def _server_class(host: str):
    if ":" not in host:
        return AppHTTPServer

    class IPv6AppHTTPServer(AppHTTPServer):
        address_family = socket.AF_INET6

    return IPv6AppHTTPServer


def run_server(config: AppConfig) -> None:
    authority: PathAuthority | None = None
    manager: JobManager | None = None
    server: AppHTTPServer | None = None
    try:
        registry = load_registry(config.plugins)
        store = Store(config.database_path)
        authority = PathAuthority(config)
        manager = JobManager(config, authority, registry, store)
        services = Services(
            config=config,
            registry=registry,
            store=store,
            authority=authority,
            jobs=manager,
            static_assets=_load_static_assets(),
        )
        server_type = _server_class(config.server.host)
        server = server_type((config.server.host, config.server.port), services)
    except (PluginError, StoreError, PathAccessError, StartupError, OSError) as exc:
        if manager is not None:
            manager.shutdown(wait=False)
        if authority is not None:
            authority.close()
        if isinstance(exc, StartupError):
            raise
        raise StartupError(str(exc)) from exc

    display_host = config.server.host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    print(
        f"File Hash Browser listening on http://{display_host}:{config.server.port} "
        f"with {config.effective_parallel_tasks} hash worker(s).",
        flush=True,
    )
    if config.server.host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "Warning: authentication is disabled; expose this listener only on a trusted network.",
            file=sys.stderr,
            flush=True,
        )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        manager.shutdown(wait=True)
        authority.close()
