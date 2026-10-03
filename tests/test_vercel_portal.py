"""The public web entrypoint must not become a private execution interface."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest.mock import patch

from app import app
from dwight.connection_catalog import PLATFORMS, build_profile

ROOT = Path(__file__).resolve().parents[1]


class VercelPortalTests(unittest.TestCase):
    def request(self, path="/", method="GET", query=""):
        captured = {}
        def start_response(status, headers):
            captured.update(status=status, headers=dict(headers))
        body = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": path,
                             "QUERY_STRING": query, "wsgi.input": io.BytesIO(b"private body")},
                            start_response))
        return captured, body

    def test_home_and_head_share_metadata_without_response_body(self):
        response, body = self.request()
        head, empty = self.request(method="HEAD")
        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(head, response)
        self.assertEqual(empty, b"")
        self.assertEqual(int(response["headers"]["Content-Length"]), len(body))
        self.assertIn(b"Dwight", body)
        self.assertNotIn(b"__VERSION__", body)
        policy = response["headers"]["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", policy)
        self.assertIn("script-src 'sha256-", policy)
        self.assertNotIn("'unsafe-inline'", policy)

    def test_health_is_public_capability_not_worker_health(self):
        for route in ("/health", "/api/health"):
            response, body = self.request(route)
            self.assertEqual(response["status"], "200 OK")
            data = json.loads(body)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["mode"], "public_setup")
            for capability in ("account_connected", "order_execution", "live_worker"):
                self.assertIs(data[capability], False)

    def test_favicon_no_content_omits_entity_headers(self):
        response, body = self.request("/favicon.ico")
        self.assertEqual(response["status"], "204 No Content")
        self.assertEqual(body, b"")
        self.assertNotIn("Content-Type", response["headers"])
        self.assertNotIn("Content-Length", response["headers"])

    def test_public_catalog_and_profile_roundtrip_all_platforms(self):
        response, body = self.request("/api/platforms")
        self.assertEqual(response["status"], "200 OK")
        catalog = json.loads(body)
        self.assertEqual(catalog["capability_schema_version"], 1)
        self.assertEqual({row["id"] for row in catalog["platforms"]}, set(PLATFORMS))
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                response, body = self.request("/api/profile/" + platform, query="feed=iex")
                self.assertEqual(response["status"], "200 OK")
                self.assertEqual(json.loads(body), build_profile(platform, "iex"))
                self.assertIn("attachment;", response["headers"]["Content-Disposition"])
                self.assertIs(json.loads(body)["execution_enabled"], False)

    def test_private_files_and_execution_routes_cannot_be_requested(self):
        for route in ("/.env", "/.git/config", "/private-data/data.csv", "/runs/account.json",
                      "/docs/product-strategy.md", "/api/orders", "/tradingview",
                      "/api/profile/../.env", "/app.py"):
            response, body = self.request(route)
            self.assertEqual(response["status"], "404 Not Found", route)
            self.assertIn("error", json.loads(body))

    def test_mutations_rejected_without_reading_request_body(self):
        class Unreadable:
            def read(self, *args):
                raise AssertionError("public portal cannot read posted credentials")
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            captured = []
            body = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": "/api/profile/alpaca",
                                 "wsgi.input": Unreadable()},
                                lambda status, headers: captured.append((status, dict(headers)))))
            self.assertEqual(captured[0][0], "405 Method Not Allowed")
            self.assertEqual(captured[0][1]["Allow"], "GET, HEAD")
            self.assertNotIn(b"private", body)

    def test_invalid_queries_are_rejected_without_reflecting_values(self):
        for query in ("feed=custom", "feed=", "feed=iex&feed=sip", "secret=private", "feed",
                      "feed=sip&url=https://example.invalid", "a=1&b=2&c=3"):
            response, body = self.request("/api/profile/alpaca", query=query)
            self.assertEqual(response["status"], "400 Bad Request", query)
            self.assertNotIn(b"private", body)
            self.assertNotIn(b"example.invalid", body)

    def test_runtime_never_reads_credentials_or_imports_research(self):
        sentinel = "PRIVATE_CREDENTIAL_MUST_NOT_APPEAR"
        with patch.dict("os.environ", {"APCA_API_KEY_ID": sentinel, "DATABENTO_API_KEY": sentinel}):
            for route in ("/", "/api/health", "/api/platforms", "/api/profile/alpaca"):
                self.assertNotIn(sentinel.encode(), self.request(route)[1])
        result = subprocess.run([sys.executable, "-c", "import app,sys,json; print(json.dumps(list(sys.modules)))"],
                                cwd=ROOT, check=True, capture_output=True, text=True)
        modules = json.loads(result.stdout)
        for prefix in ("gradio", "numpy", "sklearn", "dwight.research", "dwight.analytics",
                       "dwight.connection_checks", "dwight.config", "dwight.__main__",
                       "dwight.authorization", "dwight.paper"):
            self.assertFalse(any(module == prefix or module.startswith(prefix + ".") for module in modules), prefix)

    def test_vercel_entrypoint_and_private_bundle_exclusions_are_declared(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["tool"]["vercel"]["entrypoint"], "app:app")
        self.assertEqual(project["project"]["dependencies"], [])
        config = json.loads((ROOT / "vercel.json").read_text())
        self.assertEqual(config["framework"], "python")
        exclusions = config["functions"]["app.py"]["excludeFiles"]
        self.assertLessEqual(len(exclusions), 256)
        for private in (".*", "private-data", "runs", "models", "mlruns",
                        "internal-business", "private-notes", "docs"):
            self.assertIn(private, exclusions)
        ignore = (ROOT / ".vercelignore").read_text()
        self.assertIn("/*", ignore)
        for allowed in ("!/app.py", "!/dwight/connection_catalog.py", "!/deploy/vercel/index.html"):
            self.assertIn(allowed, ignore)


if __name__ == "__main__":
    unittest.main()
