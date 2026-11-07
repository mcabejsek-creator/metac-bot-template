import socket
import urllib.request

import dotenv
import httpx
import requests

from . import safety
from .support import OfflineTestCase, app


class SafetyTests(OfflineTestCase):
    def test_transport_guards_intercept_before_network(self):
        probes = [
            lambda: urllib.request.urlopen("https://example.invalid"),
            lambda: requests.get("https://example.invalid"),
            lambda: httpx.get("https://example.invalid"),
            lambda: socket.getaddrinfo("example.invalid", 443),
            lambda: socket.create_connection(("192.0.2.1", 443)),
        ]
        for probe in probes:
            with self.subTest(probe=probes.index(probe)), safety.expected_probe():
                with self.assertRaises(safety.OfflineViolation):
                    probe()

    async def test_async_http_and_model_are_blocked(self):
        with safety.expected_probe():
            async with httpx.AsyncClient() as client:
                with self.assertRaises(safety.OfflineViolation):
                    await client.get("https://example.invalid")
            with self.assertRaises(safety.OfflineViolation):
                await app.GeneralLlm("gpt-4o").invoke("Never send this")

    def test_dotenv_loading_is_disabled(self):
        self.assertFalse(dotenv.load_dotenv("never-read.env"))
