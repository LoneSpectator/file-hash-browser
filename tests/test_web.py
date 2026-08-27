from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from file_hash_browser.algorithms import load_registry
from file_hash_browser.jobs import JobManager
from file_hash_browser.paths import PathAuthority
from file_hash_browser.store import Store
from file_hash_browser.web import AppHTTPServer, Services, _load_static_assets
from tests.helpers import write_config


class WebBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.config, self.root = write_config(
            base,
            show_full_filename=False,
            label="Secret Authorized Root",
        )
        self.secret_directory = self.root / "Confidential Directory"
        self.secret_directory.mkdir()
        self.secret_file = self.root / "Extremely Secret Document.txt"
        self.canary = b"CANARY-CONTENT-DO-NOT-LEAK-01739"
        self.secret_file.write_bytes(self.canary)
        self.registry = load_registry(self.config.plugins)
        self.store = Store(self.config.database_path)
        self.authority = PathAuthority(self.config)
        self.manager = JobManager(self.config, self.authority, self.registry, self.store)
        services = Services(
            self.config,
            self.registry,
            self.store,
            self.authority,
            self.manager,
            _load_static_assets(),
        )
        self.server = AppHTTPServer(("127.0.0.1", 0), services)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.manager.shutdown(wait=True)
        self.authority.close()
        self.temporary.cleanup()

    def request(self, method: str, path: str, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = dict(headers or {})
        raw_body = None
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=raw_body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, data

    def test_hidden_names_and_real_paths_never_reach_bootstrap_or_listing(self) -> None:
        status, headers, body = self.request("GET", "/api/v1/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        text = body.decode("utf-8")
        self.assertNotIn("Secret Authorized Root", text)
        self.assertNotIn(str(self.root), text)
        payload = json.loads(text)
        root_node = payload["roots"][0]
        self.assertTrue(root_node["masked"])

        status, _headers, body = self.request(
            "GET", f"/api/v1/nodes/{root_node['id']}/children?offset=0&limit=20"
        )
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn("Confidential Directory", text)
        self.assertNotIn("Extremely Secret Document.txt", text)
        listed = json.loads(text)["items"]
        self.assertTrue(all(item["masked"] for item in listed))
        self.assertTrue(all("…" in item["displayName"] for item in listed))

    def test_no_route_downloads_file_content_or_unauthorized_digest(self) -> None:
        candidate_paths = [
            "/files/Extremely%20Secret%20Document.txt",
            "/api/v1/download?path=Extremely%20Secret%20Document.txt",
            "/../authorized/Extremely%20Secret%20Document.txt",
        ]
        forbidden = [
            self.canary,
            hashlib.md5(self.canary, usedforsecurity=False).hexdigest().encode(),
            hashlib.sha1(self.canary, usedforsecurity=False).hexdigest().encode(),
            hashlib.sha256(self.canary).hexdigest().encode(),
            hashlib.sha512(self.canary).hexdigest().encode(),
        ]
        for path in candidate_paths:
            with self.subTest(path=path):
                status, _headers, body = self.request("GET", path)
                self.assertEqual(status, 404)
                for value in forbidden:
                    self.assertNotIn(value, body)

    def test_cross_origin_job_creation_is_rejected(self) -> None:
        status, headers, _body = self.request(
            "POST",
            "/api/v1/jobs",
            body={"items": [], "algorithmIds": ["sha256"], "strategy": "missing-only"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(headers.get("Connection"), "close")

    def test_delete_and_clear_active_jobs(self) -> None:
        first, _ = self.store.create_job(
            job_id=uuid.uuid4().hex,
            idempotency_key=None,
            strategy="missing-only",
            algorithm_ids=("sha256",),
            selected_count=1,
            max_active_jobs=8,
        )
        second, _ = self.store.create_job(
            job_id=uuid.uuid4().hex,
            idempotency_key=None,
            strategy="missing-only",
            algorithm_ids=("sha256",),
            selected_count=1,
            max_active_jobs=8,
        )
        status, _headers, _body = self.request("DELETE", f"/api/v1/jobs/{first.id}")
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.get_job(first.id))
        status, _headers, body = self.request("DELETE", "/api/v1/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["removed"], 1)
        self.assertIsNone(self.store.get_job(second.id))


if __name__ == "__main__":
    unittest.main()
