"""Vercel entrypoint: load the real app, or explain why it could not load.

A serverless Python function that dies during import produces
`FUNCTION_INVOCATION_FAILED` and an empty body. No traceback reaches the caller,
so from outside the deployment a missing dependency, a wrong Python version and a
genuine bug are indistinguishable — a miserable way to debug something reachable
only over HTTP. So the import is guarded, and on failure `app` becomes a minimal
ASGI application that answers every request with the traceback and enough
environment detail to identify the cause.

One structural rule matters more than it looks. Vercel decides whether a file in
`api/` is a function by looking for a **top-level** name — `app`, `application`
or `handler`. A binding that only exists inside a `try`/`except` body is not
top-level, and a file without one is not treated as a function at all; the build
then fails with "The pattern ... doesn't match any Serverless Functions inside
the `api` directory". Hence the shape below: the guarded import writes to a
private name, and the last statement in this module is a plain unconditional
`app = ...` assignment that no detector can miss.
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
_loaded_app = None

try:
    from praxis.api import app as _loaded_app
except Exception:  # noqa: BLE001 - reporting anything at all is the point
    BOOT_TRACEBACK = traceback.format_exc()


def _installed_packages():
    """Names of what actually got installed, to spot a missing requirement."""
    try:
        from importlib import metadata

        return sorted(
            "{0}=={1}".format(distribution.metadata["Name"], distribution.version)
            for distribution in metadata.distributions()
            if distribution.metadata.get("Name")
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
            "entrypoint_directory_contents": sorted(
                os.listdir(os.path.dirname(os.path.abspath(__file__)))
            ),
            "sys_path": sys.path,
            "installed_packages": _installed_packages(),
            "hint": (
                "A ModuleNotFoundError for a third-party package means "
                "api/requirements.txt was not installed. One for 'praxis' means "
                "the package directory was not bundled with the function."
            ),
        },
        indent=1,
    ).encode()


async def _diagnostic_app(scope, receive, send):
    """Minimal ASGI app: the same readable failure on every route."""
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


# Unconditional, top-level, and the last word in this module: this is the name
# Vercel binds, and it must be visible without executing a branch.
app = _loaded_app if _loaded_app is not None else _diagnostic_app
