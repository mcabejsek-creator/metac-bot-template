"""Process-local safety barriers; no credentials, dotenv files, or live transports.

The supported runner also fails on caught/hidden guard violations, including in
known-bug tests. Never import this package from production application code.
"""

import os
import socket
from contextlib import contextmanager
from unittest.mock import patch


class OfflineViolation(RuntimeError):
    pass


unexpected_attempts = []
expected_attempts = []
_probe_depth = 0
_patches = []
sdk_model_invoke = None


def forbidden(boundary):
    def block(*args, **kwargs):
        # Record boundary names only, never URLs, headers, prompts or credentials.
        (expected_attempts if _probe_depth else unexpected_attempts).append(boundary)
        raise OfflineViolation(f"Live boundary disabled in offline tests: {boundary}")

    return block


async def forbidden_model(*args, **kwargs):
    return forbidden("model.invoke")()


def _start(target, **kwargs):
    guard = patch(target, **kwargs)
    guard.start()
    _patches.append(guard)


def install():
    if _patches:
        return
    # Sanitize only this test process, before SDK initialization. Do not discover
    # credentials or read .env. Test-specific synthetic tokens use patch.dict.
    for name in list(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN", "_SECRET")) or name == "ASKNEWS_CLIENT_ID":
            os.environ.pop(name, None)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["LITELLM_TELEMETRY"] = "False"
    for name in ("connect", "connect_ex", "sendto"):
        _start(f"socket.socket.{name}", new=forbidden(f"socket.{name}"))
    for name in ("create_connection", "getaddrinfo"):
        _start(f"socket.{name}", new=forbidden(f"socket.{name}"))
    # Block inherited/connected Internet sockets too; permit asyncio's Unix
    # socketpair used only for local event-loop wakeups.
    for name in ("send", "sendall"):
        original = getattr(socket.socket, name)

        def send(sock, *args, _original=original, _name=name, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                return forbidden(f"socket.{_name}")()
            return _original(sock, *args, **kwargs)

        _start(f"socket.socket.{name}", new=send)
    _start("urllib.request.urlopen", new=forbidden("urllib.urlopen"))
    _start("requests.sessions.Session.request", new=forbidden("requests.request"))
    _start("httpx.Client.send", new=forbidden("httpx.send"))

    async def async_send(*args, **kwargs):
        return forbidden("httpx.async_send")()

    _start("httpx.AsyncClient.send", new=async_send)
    _start("dotenv.load_dotenv", return_value=False)


def guard_models():
    # Called only after guarded SDK import. Individual tests replace this with
    # explicit deterministic model text; the underlying transports stay blocked.
    global sdk_model_invoke
    from forecasting_tools import GeneralLlm
    sdk_model_invoke = GeneralLlm.invoke
    _start("forecasting_tools.GeneralLlm.invoke", new=forbidden_model)
    _start("forecasting_tools.GeneralLlm._mockable_direct_call_to_model", new=forbidden_model)


@contextmanager
def expected_probe():
    """Only safety self-tests may intentionally exercise a blocked boundary."""
    global _probe_depth
    _probe_depth += 1
    try:
        yield
    finally:
        _probe_depth -= 1
