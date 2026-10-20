import http.client
import json
import threading
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from app import make_server


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory()
        cls.settings_path = Path(cls.workspace.name) / "config.local.json"
        cls.server = make_server(0, cls.settings_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.workspace.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_page_assets_and_template(self):
        for path in ("/", "/app.js", "/styles.css", "/api/template"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_status_never_claims_remote_connections(self):
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["can_create_ttn"])

    def test_settings_roundtrip_does_not_enable_connections(self):
        payload = json.dumps({"settings": {"didox_url": "https://didox.uz/", "smartup_url": "https://smartup.example.test/", "company_tin": "012345678", "loading_address": "Test address"}}).encode()
        status, _, body = self.request("POST", "/api/settings", payload, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        saved = json.loads(body)["settings"]
        self.assertEqual(saved["company_tin"], "012345678")
        self.assertEqual(json.loads(self.request("GET", "/api/settings")[2])["settings"], saved)
        flags = json.loads(self.request("GET", "/api/status")[2])
        self.assertFalse(flags["didox_connected"])
        self.assertFalse(flags["smartup_connected"])
        self.assertFalse(flags["can_create_ttn"])
        self.assertEqual(self.request("GET", "/config.local.json")[0], 404)

    def test_invalid_settings_and_cross_origin_save_do_not_change_file(self):
        before = self.settings_path.read_bytes() if self.settings_path.exists() else None
        payload = json.dumps({"settings": {"smartup_url": "javascript:alert(1)"}}).encode()
        self.assertEqual(self.request("POST", "/api/settings", payload)[0], 400)
        self.assertEqual(self.request("POST", "/api/settings", b'{}', {"Origin": "https://unrelated.example"})[0], 403)
        after = self.settings_path.read_bytes() if self.settings_path.exists() else None
        self.assertEqual(before, after)

    def test_upload_validate_and_export_end_to_end(self):
        payload = 'Номер СФ;Дата СФ;ИНН клиента\r\n00078/11;12.10.2026;012345678\r\n'.encode("utf-8-sig")
        status, _, body = self.request("POST", "/api/import?filename=" + quote("Список.csv"), payload, {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        imported = json.loads(body)
        self.assertEqual(imported["summary"]["ready"], 1)
        body = json.dumps({"rows": imported["rows"]}).encode()
        status, _, result = self.request("POST", "/api/validate", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["ttn_created"], 0)
        body = json.dumps({"kind": "list", "rows": imported["rows"], "selected_ids": ["row-2"]}).encode()
        status, headers, output = self.request("POST", "/api/export", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn(".xlsx", headers["Content-Disposition"])
        self.assertTrue(output.startswith(b"PK"))

    def test_wrong_origin_and_dns_rebinding_host_are_denied(self):
        status, _, _ = self.request("POST", "/api/validate", b"{}", {"Origin": "https://unrelated.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/", headers={"Host": "unrelated.example"})
        self.assertEqual(status, 403)

    def test_paths_do_not_expose_workspace_files(self):
        for path in ("/../README.txt", "/invoice_processor.py", "/api/create-ttn"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_invalid_payload_returns_a_clear_error(self):
        status, _, body = self.request("POST", "/api/validate", b"not-json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main()
