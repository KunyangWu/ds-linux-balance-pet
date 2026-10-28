import json
import os
import sys
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linux"))

from ds_pet.balance import BalanceError, fetch_balance
from ds_pet.config import Config


class Handler(BaseHTTPRequestHandler):
    status = 200
    payload = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "18.47",
                "topped_up_balance": "10.00",
                "granted_balance": "8.47",
            }
        ],
    }
    authorization = "Bearer test-key"

    def do_GET(self):
        if self.path != "/user/balance":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("Authorization") != self.authorization:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


warnings.simplefilter("ignore", ResourceWarning)


class BalanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/user/balance"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.socket.close()
        cls.thread.join(2)

    def config(self, **changes):
        values = {
            "api_key": "test-key",
            "balance_url": self.url,
            "request_timeout": 2,
            "low_threshold": None,
        }
        values.update(changes)
        return Config(**values)

    def test_balance_success(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "BALANCE_URL": ""}):
            result = fetch_balance(self.config())
        self.assertEqual(result.text, "¥18.47")
        self.assertFalse(result.low)
        self.assertEqual(result.currency, "CNY")
        self.assertIn("充值 ¥10.00", result.detail)

    def test_threshold_marks_low(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "BALANCE_URL": ""}):
            result = fetch_balance(self.config(low_threshold=20))
        self.assertTrue(result.low)

    def test_missing_key_is_safe(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "BALANCE_URL": ""}):
            with self.assertRaises(BalanceError) as context:
                fetch_balance(self.config(api_key=""))
        self.assertIn("未配置", str(context.exception))

    def test_http_error_is_safe(self):
        original = Handler.authorization
        Handler.authorization = "Bearer other"
        try:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "BALANCE_URL": ""}):
                with self.assertRaises(BalanceError) as context:
                    fetch_balance(self.config())
            self.assertIn("HTTP 401", str(context.exception))
        finally:
            Handler.authorization = original

    def test_invalid_payload(self):
        original = Handler.payload
        Handler.payload = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "x"}]}
        try:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "", "BALANCE_URL": ""}):
                with self.assertRaises(BalanceError):
                    fetch_balance(self.config())
        finally:
            Handler.payload = original


if __name__ == "__main__":
    unittest.main()
