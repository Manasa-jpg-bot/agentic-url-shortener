import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from shortener import Store, make_handler, validate_url


class ShortenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "test.db"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        encoded = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=encoded, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_end_to_end_and_persistence(self):
        status, _, data = self.request("POST", "/api/links", {"url": "https://example.org/a?q=1", "code": "sample"})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(data)["code"], "sample")
        status, headers, _ = self.request("GET", "/sample")
        self.assertEqual((status, headers["Location"]), (302, "https://example.org/a?q=1"))
        self.assertEqual(Store(self.store.path).stats("sample")["clicks"], 1)
        status, _, data = self.request("GET", "/api/links/sample/stats")
        self.assertEqual(json.loads(data)["clicks"], 1)

    def test_invalid_and_conflicting_links(self):
        for url in ("javascript:alert(1)", "file:///tmp/a", "https://user:pass@example.org", "https://example.org/\nheader"):
            with self.subTest(url=url):
                self.assertEqual(self.request("POST", "/api/links", {"url": url})[0], 400)
        self.assertEqual(self.request("POST", "/api/links", {"url": "https://example.org", "code": "same"})[0], 201)
        self.assertEqual(self.request("POST", "/api/links", {"url": "https://example.net", "code": "same"})[0], 400)
        self.assertEqual(self.request("GET", "/unknown")[0], 404)

    def test_brownfield_delete_if_available(self):
        if not hasattr(self.store, "delete"):
            self.skipTest("delete enhancement belongs to brownfield artifact")
        self.assertEqual(self.request("POST", "/api/links", {"url": "https://example.org", "code": "todelete"})[0], 201)
        self.assertEqual(self.request("DELETE", "/api/links/todelete")[0], 204)
        self.assertEqual(self.request("GET", "/todelete")[0], 404)
        self.assertEqual(self.request("DELETE", "/api/links/todelete")[0], 404)


if __name__ == "__main__":
    unittest.main()
