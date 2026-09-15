#!/usr/bin/env python3

import argparse
import base64
import hmac
import html
import os
import posixpath
import urllib.parse

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from gitignore_parser import parse_gitignore

ENV_ROOT = "FAST_HOST_ROOT"
ENV_TOKEN = "FAST_HOST_TOKEN"
ENV_IGNORE = "FAST_HOST_IGNOREFILES"


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _tokens_match(got, expected):
    if not got:
        return False
    try:
        return hmac.compare_digest(got, expected)
    except (TypeError, ValueError):
        return False


def _authorized(request, token):
    if token is None:
        return True

    if _tokens_match(request.query_params.get("token"), token):
        return True

    if _tokens_match(request.headers.get("x-token"), token):
        return True

    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and _tokens_match(
        auth.split(" ", 1)[1].strip(), token
    ):
        return True

    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        user, _, password = decoded.partition(":")
        if _tokens_match(password, token) or _tokens_match(user, token):
            return True

    return False


def _safe_join(root, url_path):
    relative = posixpath.normpath(url_path).lstrip("/")
    if relative in (".", ""):
        candidate = os.path.realpath(root)
    else:
        candidate = os.path.realpath(os.path.join(root, *relative.split("/")))
    root = os.path.realpath(root)
    try:
        common = os.path.commonpath([root, candidate])
    except ValueError:
        return None
    if common != root:
        return None
    return candidate


def _ignored(ignore, path):
    if ignore is None:
        return False
    try:
        return bool(ignore(path))
    except ValueError:
        return False


def _relative_posix(root, target):
    rel = os.path.relpath(target, root)
    if rel == ".":
        return ""
    return rel.replace(os.sep, "/")


def _listing_page(rel_posix, directory, ignore):
    try:
        names = os.listdir(directory)
    except OSError:
        return None

    entries = []
    for name in sorted(names, key=str.lower):
        full = os.path.join(directory, name)
        child_is_dir = os.path.isdir(full)
        if _ignored(ignore, full):
            continue
        href_name = urllib.parse.quote(name, safe="")
        if child_is_dir:
            href_name += "/"
            display = name + "/"
        else:
            display = name
        entries.append(
            f'<li><a href="{html.escape(href_name, quote=True)}">'
            f"{html.escape(display)}</a></li>"
        )

    title = "/" + rel_posix if rel_posix else "/"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Index of {html.escape(title)}</title></head><body>"
        f"<h1>Index of {html.escape(title)}</h1><ul>"
        '<li><a href="../">../</a></li>'
        + "".join(entries)
        + "</ul></body></html>"
    )


def create_app():
    root = os.path.realpath(os.path.abspath(_env(ENV_ROOT, ".")))
    token = _env(ENV_TOKEN)
    ignorefiles = _env(ENV_IGNORE)
    ignore = (
        parse_gitignore(os.path.abspath(ignorefiles), base_dir=root)
        if ignorefiles
        else None
    )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not _authorized(request, token):
            return Response(
                content=b"Unauthorized\n",
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="fast_host"',
                    "Content-Type": "text/plain; charset=utf-8",
                },
            )
        return await call_next(request)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def serve(path: str, request: Request):
        target = _safe_join(root, path)
        if target is None:
            return Response(status_code=404)

        rel_posix = _relative_posix(root, target)
        if rel_posix and _ignored(ignore, target):
            return Response(status_code=404)

        if os.path.isdir(target):
            if path and not request.url.path.endswith("/"):
                query = request.url.query
                location = request.url.path + "/"
                if query:
                    location += "?" + query
                return RedirectResponse(location, status_code=301)
            page = _listing_page(rel_posix, target, ignore)
            if page is None:
                return Response(status_code=404)
            return HTMLResponse(page)

        if not os.path.isfile(target):
            return Response(status_code=404)

        return FileResponse(target)

    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(
        description="Host a directory over HTTP with FastAPI (reload always on)."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to serve (default: current directory)",
    )
    parser.add_argument("--port", type=int, default=8888, help="(default: 8888)")
    parser.add_argument("--host", default="0.0.0.0", help="(default: 0.0.0.0)")
    parser.add_argument(
        "--token",
        default=None,
        help="Shared secret via Basic user/password, Bearer, X-Token, or ?token=",
    )
    parser.add_argument(
        "--ignorefiles",
        default=None,
        help="gitignore file of paths to hide",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        raise SystemExit(f"Directory does not exist: {root}")
    if args.ignorefiles and not os.path.isfile(args.ignorefiles):
        raise SystemExit(f"Ignore file does not exist: {args.ignorefiles}")

    os.environ[ENV_ROOT] = root
    if args.token:
        os.environ[ENV_TOKEN] = args.token
    elif ENV_TOKEN in os.environ:
        del os.environ[ENV_TOKEN]
    if args.ignorefiles:
        os.environ[ENV_IGNORE] = os.path.abspath(args.ignorefiles)
    elif ENV_IGNORE in os.environ:
        del os.environ[ENV_IGNORE]

    here = os.path.dirname(os.path.abspath(__file__))
    print(
        f"Serving {root} on http://{args.host}:{args.port}/ (reload=True)",
        flush=True,
    )
    uvicorn.run(
        "fast_host:app",
        host=args.host,
        port=args.port,
        reload=True,
        app_dir=here,
        reload_dirs=[here],
    )


if __name__ == "__main__":
    main()

# fastapi.staticfiles.StaticFiles is not usable here:
# - html=True listings omit the trailing slash on directories, so http_fs.py
#   treats every entry as a file (it uses href.endswith("/")).
# - it has no --ignorefiles / gitignore filter (hidden paths would still list
#   and GET).
# - mounting it at "/" swallows the app, so token checks and the /dir -> /dir/
#   redirect cannot sit in front of it.
