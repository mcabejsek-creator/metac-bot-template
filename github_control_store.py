"""Dormant GitHub Contents persistence adapter for dispatch control ONLY.

Not imported by any production workflow or CLI. No branch is created.
The target branch (metaculus-dispatch-control) must be separately authorized
and seeded DISABLED before any future use. This adapter has no workflow
dispatch, Metaculus API, publication journal, or model access.

GitHub Contents update requires the current file blob SHA and may return
409 on concurrent modification. On ANY ambiguity refuse to issue a dispatch.
"""

import base64
import binascii
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from dispatch_intent_controller import ControllerState, InvalidControlState

TARGET_REPO = "mcabejsek-creator/metac-bot-template"
TARGET_BRANCH = "metaculus-dispatch-control"  # NOT created by this proposal
TARGET_PATH = "dispatch-controller.json"
API_URL = f"https://api.github.com/repos/{TARGET_REPO}/contents/{TARGET_PATH}"
_SHA = re.compile(r"[0-9a-f]{40}")


class ControlStoreUnavailable(RuntimeError):
    """No trusted state/revision, so absolutely no automatic dispatch."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ControlStoreUnavailable("GitHub redirected; no control write permitted")


class GitHubContentsControlStore:
    """Strict GitHub Content SHA compare-and-swap for controller-only state.

    Caller must NOT automatically retry a 409/conflict or a network timeout
    after PUT. The latter might have committed despite a lost response.
    A future authorized controller must re-read and reconcile first.
    """

    def __init__(self, *, token, opener=None):
        if not isinstance(token, str) or not token.strip() or token != token.strip():
            raise ControlStoreUnavailable("GitHub control credential missing")
        self._token = token
        self._opener = opener if opener is not None else urllib.request.build_opener(_NoRedirects())

    def _request(self, method, *, url=API_URL, body=None):
        if url not in (API_URL, API_URL + "?ref=" + TARGET_BRANCH):
            raise ControlStoreUnavailable("Control store URL not allowlisted")
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vrchcab-dormant-dispatch-controller",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(
            url, data=None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers, method=method,
        )

    def read(self):
        """Read only the separate control branch; absence FAILS CLOSED."""
        req = self._request("GET", url=API_URL + "?ref=" + TARGET_BRANCH)
        try:
            with self._opener.open(req, timeout=12) as response:
                if response.status != 200:
                    raise ControlStoreUnavailable("Unexpected GitHub control GET status")
                raw = response.read(12001)
                if len(raw) > 12000:
                    raise ControlStoreUnavailable("Control response too large")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as error:
            raise ControlStoreUnavailable("GitHub control GET unavailable") from None
        try:
            info = json.loads(raw)
            if (not isinstance(info, dict)
                    or info.get("path") != TARGET_PATH
                    or info.get("type") != "file"):
                raise InvalidControlState("Unexpected control resource")
            sha = info["sha"]
            if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise InvalidControlState("Unknown control file revision")
            if info["encoding"] != "base64" or not isinstance(info["content"], str):
                raise InvalidControlState("Unknown controller file encoding")
            binary = base64.b64decode(info["content"], validate=False)
            state = ControllerState.decode(binary)
        except (ValueError, TypeError, KeyError, binascii.Error, InvalidControlState):
            raise ControlStoreUnavailable("Malformed GitHub control metadata") from None
        return state, sha

    def compare_and_swap(self, expected_revision, new_state):
        """Return TRUE only after confirmed 200 OK; conflict = FALSE.

        This adapter NEVER creates or enables a control branch or file.
        It may ONLY update an existing blob on TARGET_BRANCH.
        """
        if not isinstance(expected_revision, str) or not _SHA.fullmatch(expected_revision):
            raise ControlStoreUnavailable("Expected control blob SHA required")
        if not isinstance(new_state, ControllerState):
            raise ControlStoreUnavailable("Validated new state required")
        payload = {
            "branch": TARGET_BRANCH,
            "sha": expected_revision,
            "message": "Record dispatch-control state without triggering forecasts",
            "content": base64.b64encode(new_state.encode().encode("utf-8")).decode("ascii"),
        }
        req = self._request("PUT", body=payload)
        try:
            with self._opener.open(req, timeout=12) as response:
                if response.status not in (200,):
                    raise ControlStoreUnavailable("Unknown GitHub control update status")
                raw = response.read(12001)
                if len(raw) > 12000:
                    raise ControlStoreUnavailable("Unexpected large control write response")
        except urllib.error.HTTPError as error:
            if error.code in (409, 422):
                return False
            raise ControlStoreUnavailable("GitHub control PUT rejected") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            # May already be stored. No auto-retry and certainly NO dispatch.
            raise ControlStoreUnavailable("Ambiguous GitHub control PUT; reconcile manually") from None
        try:
            info = json.loads(raw)
            file_info = info["content"]
            if (not isinstance(file_info, dict)
                    or file_info.get("path") != TARGET_PATH
                    or not isinstance(file_info.get("sha"), str)
                    or not _SHA.fullmatch(file_info["sha"])):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ControlStoreUnavailable("GitHub control PUT acknowledgment unverified") from None
        return True
