"""Public, read-only WSGI entrypoint for Vercel and local previews.

This portal never loads private environment files, account state or experiments.
The continuously running trading worker is a separate private installation.
"""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs

from dwight import __version__
from dwight.connection_catalog import CAPABILITY_SCHEMA_VERSION, PLATFORMS, build_profile


PAGE = Path(__file__).resolve().parent / "deploy" / "vercel" / "index.html"
CAPABILITIES = {
    "mode": "public_setup",
    "account_connected": False,
    "order_execution": False,
    "live_worker": False,
}


def _json(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")


def _page():
    body = PAGE.read_text(encoding="utf-8").replace("__VERSION__", __version__)
    # Hash only the reviewed inline assets, rather than allowing arbitrary script.
    import base64
    hashes = {}
    for tag in ("script", "style"):
        hashes[tag] = " ".join(
            "'sha256-" + base64.b64encode(hashlib.sha256(asset.encode()).digest()).decode() + "'"
            for asset in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", body, re.S)
        )
    policy = (
        "default-src 'none'; connect-src 'self'; img-src 'self' data:; "
        f"script-src {hashes['script']}; style-src {hashes['style']}; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    )
    return body.encode("utf-8"), policy


def app(environ, start_response):
    """Serve fixed public routes without reading input bodies or private state."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")
    status, body, content_type = "200 OK", b"", "application/json; charset=utf-8"
    headers = []
    if method not in ("GET", "HEAD"):
        status, body = "405 Method Not Allowed", _json({"error": "Read-only portal"})
        headers.append(("Allow", "GET, HEAD"))
    elif path == "/" and not query:
        body, policy = _page()
        content_type = "text/html; charset=utf-8"
        headers.append(("Content-Security-Policy", policy))
    elif path in ("/health", "/api/health") and not query:
        body = _json({"status": "ok", "version": __version__, **CAPABILITIES})
    elif path == "/api/platforms" and not query:
        body = _json({"version": __version__, "capability_schema_version": CAPABILITY_SCHEMA_VERSION,
                      **CAPABILITIES, "platforms": list(PLATFORMS.values())})
    elif path.startswith("/api/profile/"):
        platform = path.removeprefix("/api/profile/")
        if platform not in PLATFORMS:
            status, body = "404 Not Found", _json({"error": "Unknown platform"})
        else:
            try:
                params = parse_qs(query, keep_blank_values=True, max_num_fields=2, strict_parsing=True)
                if set(params) - {"feed"} or len(params.get("feed", [])) > 1:
                    raise ValueError("Only one feed parameter is supported")
                profile = build_profile(platform, params.get("feed", ["sip"])[0])
                body = _json(profile)
                headers.append(("Content-Disposition", f'attachment; filename="dwight-{platform}.json"'))
            except ValueError:
                status, body = "400 Bad Request", _json({"error": "Choose feed=sip or feed=iex"})
    elif path == "/favicon.ico" and not query:
        status = "204 No Content"
    else:
        status, body = "404 Not Found", _json({"error": "Not found"})
    if status != "204 No Content":
        headers.extend([("Content-Type", content_type), ("Content-Length", str(len(body)))])
    headers.extend([
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "SAMEORIGIN"),
    ])
    start_response(status, headers)
    return [b"" if method == "HEAD" else body]


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    with make_server("127.0.0.1", 8080, app) as server:
        print("Dwight public setup preview: http://127.0.0.1:8080", flush=True)
        server.serve_forever()
