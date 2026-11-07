"""Compare historical audit request method with installed SDK, OFFLINE ONLY.

This script makes zero network requests, reads no token and never imports main.py.
It reports only HTTP method names and source code presence.
"""
import ast
import inspect
from pathlib import Path

from forecasting_tools.helpers.metaculus_client import MetaculusClient

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "tools" / "audit_metaculus_history.py"


def function_calls_http_methods(fn):
    source = inspect.getsource(fn)
    tree = ast.parse(inspect.cleandoc(source))
    methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("post", "get", "request")
    }
    return source, sorted(methods)


def main():
    sdk_fn = getattr(MetaculusClient, "_get_acting_user_forecasts", None)
    if sdk_fn is None:
        print("Installed SDK does not expose _get_acting_user_forecasts; review required.")
        return
    sdk_source, sdk_methods = function_calls_http_methods(sdk_fn)
    audit_content = AUDIT_PATH.read_text(encoding="utf-8")
    sdk_mentions_endpoint = "bulk-forecast-read" in sdk_source
    audit_mentions_endpoint = "bulk-forecast-read" in audit_content
    audit_uses_get = 'method="GET"' in audit_content
    print("SDK acting-user forecast history methods:", sdk_methods)
    print("SDK references bulk-forecast-read:", sdk_mentions_endpoint)
    print("Audit references bulk-forecast-read:", audit_mentions_endpoint)
    print("Audit uses urllib GET only:", audit_uses_get)
    if sdk_mentions_endpoint and audit_mentions_endpoint and "post" in sdk_methods and audit_uses_get:
        print("MISMATCH: SDK calls the read endpoint with POST while audit sends GET.")
        print("Do not change audit to POST or rerun it without separate review/authorization.")
    else:
        print("No conclusive SDK-vs-audit method mismatch; inspect exact installed code.")


if __name__ == "__main__":
    main()
