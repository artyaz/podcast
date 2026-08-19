"""Vercel entrypoint: load the real app, or explain why it could not load.

A serverless Python function that dies during import produces
`FUNCTION_INVOCATION_FAILED` and an empty body. No traceback reaches the caller,
so from outside the deployment a missing dependency, a wrong Python version, and
a genuine bug are indistinguishable — which is a miserable way to debug something
you can only reach over HTTP.

So the import is guarded. When it succeeds, `app` is the real FastAPI
application and this file does nothing else. When it fails, `app` becomes a
minimal ASGI application that answers every request with the traceback and
enough environment detail to identify the cause. That turns a blank 500 into a
readable one.
"""

import json
import os
import sys
import traceback

# The `praxis` package sits next to this file, but Vercel runs functions with the
# project root as the working directory, so a bare `import praxis` is not
# guaranteed to resolve. Putting this file's own directory first makes the import
# behave identically locally, under `vercel dev`, and when deployed.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOOT_TRACEBACK = None

try:
    from praxis.api import app
except Exception:  # noqa: BLE001 - the whole point is to report anything
    BOOT_TRACEBACK = traceback.format_exc()

    def _installed_packages():
        """Names of what actually got installed, to spot a missing requirement."""
        try:
            from importlib import metadata

            return sorted(
                "{0}=={1}".format(dist.metadata["Name"], dist.version)
                for dist in metadata.distributions()
                if dist.metadata.get("Name")
            )
        except Exception as listing_error:  # noqa: BLE001
            return ["could not list packages: {0}".format(listing_error)]

    def _diagnostic_body() -> bytes:
        return json.dumps(
            {
                "ok": False,
                "error": "The Praxis backend failed to import.",
                "traceback": BOOT_TRACEBACK,
                "python_version": sys.version,
                "working_directory": os.getcwd(),
                "entrypoint_directory": os.path.dirname(os.path.abspath(__file__)),
                "sys_path": sys.path,
                "installed_packages": _installed_packages(),
                "hint": (
                    "A missing package usually means requirements.txt is not where "
                    "the build looked for it — it belongs at the project root, not "
                    "inside api/. A ModuleNotFoundError for 'praxis' means the "
                    "package directory did not get bundled."
                ),
            },
            indent=1,
        ).encode()

    async def app(scope, receive, send):
        """Minimal ASGI app: same readable failure for every route."""
        if scope["type"] != "http":
            return
        body = _diagnostic_body()
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"access-control-allow-origin", b"*"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
