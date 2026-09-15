#!/usr/bin/env python3

import argparse
import base64
import html
import hmac
import mimetypes
import os
import posixpath
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gitignore_parser import parse_gitignore


def _tokens_match(got, expected):
    if not got:
        return False
    try:
        return hmac.compare_digest(got, expected)
    except (TypeError, ValueError):
        return False


class StaticHandler(BaseHTTPRequestHandler):
    root = None
    token = None
    ignore = None

    def do_HEAD(self):
        self._handle(body=False)

    def do_GET(self):
        self._handle(body=True)

    def log_message(self, fmt, *args):
        message = fmt % args
        message = re.sub(r"([?&]token=)[^&\s]+", r"\1***", message)
        sys_stderr_write = super().log_message
        sys_stderr_write("%s", message)

    def _handle(self, body):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="static_host"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if body:
                self.wfile.write(b"Unauthorized\n")
            return

        parsed = urllib.parse.urlparse(self.path)
        rel = urllib.parse.unquote(parsed.path)
        target = self._safe_join(rel)
        if target is None:
            self._send_error(404, body)
            return

        rel_posix = _relative_posix(self.root, target)
        is_dir = os.path.isdir(target)
        if rel_posix and self._ignored(target):
            self._send_error(404, body)
            return

        if is_dir:
            if not parsed.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", parsed.path + "/" + (("?" + parsed.query) if parsed.query else ""))
                self.end_headers()
                return
            self._send_listing(rel_posix, target, body)
            return

        if not os.path.isfile(target):
            self._send_error(404, body)
            return

        self._send_file(target, body)

    def _authorized(self):
        if self.token is None:
            return True

        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if _tokens_match((query.get("token") or [None])[0], self.token):
            return True

        header = self.headers.get("X-Token", "")
        if _tokens_match(header, self.token):
            return True

        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer ") and _tokens_match(
            auth.split(" ", 1)[1].strip(), self.token
        ):
            return True

        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            except Exception:
                return False
            user, _, password = decoded.partition(":")
            if _tokens_match(password, self.token) or _tokens_match(user, self.token):
                return True

        return False

    def _safe_join(self, url_path):
        relative = posixpath.normpath(url_path).lstrip("/")
        if relative in (".", ""):
            candidate = os.path.realpath(self.root)
        else:
            candidate = os.path.realpath(
                os.path.join(self.root, *relative.split("/"))
            )
        root = os.path.realpath(self.root)
        try:
            common = os.path.commonpath([root, candidate])
        except ValueError:
            return None
        if common != root:
            return None
        return candidate

    def _ignored(self, path):
        if self.ignore is None:
            return False
        try:
            return bool(self.ignore(path))
        except ValueError:
            return False

    def _send_listing(self, rel_posix, directory, body):
        try:
            names = os.listdir(directory)
        except OSError:
            self._send_error(404, body)
            return

        entries = []
        for name in sorted(names, key=str.lower):
            full = os.path.join(directory, name)
            child_is_dir = os.path.isdir(full)
            if self._ignored(full):
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
        page = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>Index of {html.escape(title)}</title></head><body>"
            f"<h1>Index of {html.escape(title)}</h1><ul>"
            '<li><a href="../">../</a></li>'
            + "".join(entries)
            + "</ul></body></html>"
        )
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def _send_file(self, path, body):
        try:
            file_size = os.path.getsize(path)
            handle = open(path, "rb")
        except OSError:
            self._send_error(404, body)
            return

        try:
            range_header = self.headers.get("Range")
            span = None
            if range_header:
                try:
                    span = _parse_byte_range(range_header, file_size)
                except _RangeError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

            content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

            if span is None:
                start, end = 0, file_size - 1 if file_size else 0
                self.send_response(200)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_size))
                self.end_headers()
                if body and file_size:
                    _copy_range(handle, self.wfile, 0, file_size)
                return

            start, end = span
            length = end - start + 1
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if body:
                _copy_range(handle, self.wfile, start, length)
        finally:
            handle.close()

    def _send_error(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()


class _RangeError(Exception):
    pass


def _parse_byte_range(header, file_size):
    kind, _, spec = header.strip().partition("=")
    if kind.strip().lower() != "bytes" or not spec or "," in spec:
        raise _RangeError

    spec = spec.strip()
    if spec.startswith("-"):
        suffix = int(spec[1:])
        if suffix <= 0:
            raise _RangeError
        start = max(file_size - suffix, 0)
        end = file_size - 1
    elif spec.endswith("-"):
        start = int(spec[:-1])
        end = file_size - 1
    else:
        start_s, end_s = spec.split("-", 1)
        start = int(start_s)
        end = int(end_s)

    if file_size == 0:
        raise _RangeError
    if start < 0 or end < start or start >= file_size:
        raise _RangeError
    end = min(end, file_size - 1)
    return start, end


def _copy_range(handle, output, start, length):
    handle.seek(start)
    remaining = length
    while remaining > 0:
        chunk = handle.read(min(remaining, 64 * 1024))
        if not chunk:
            break
        output.write(chunk)
        remaining -= len(chunk)


def _relative_posix(root, target):
    rel = os.path.relpath(target, root)
    if rel == ".":
        return ""
    return rel.replace(os.sep, "/")


def make_handler(root, token, ignore):
    class BoundHandler(StaticHandler):
        pass

    BoundHandler.root = os.path.realpath(root)
    BoundHandler.token = token
    BoundHandler.ignore = staticmethod(ignore) if ignore is not None else None
    return BoundHandler


def build_server(directory, host="0.0.0.0", port=8888, token=None, ignorefiles=None):
    root = os.path.abspath(directory)
    if not os.path.isdir(root):
        raise SystemExit(f"Directory does not exist: {root}")

    ignore = (
        parse_gitignore(os.path.abspath(ignorefiles), base_dir=root)
        if ignorefiles
        else None
    )
    handler = make_handler(root, token, ignore)
    return ThreadingHTTPServer((host, port), handler)


def main():
    parser = argparse.ArgumentParser(
        description="Host a directory over HTTP (HTML listings, Range, optional token)."
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
        help="gitignore file of paths to hide (https://github.com/mherrmann/gitignore_parser)",
    )
    args = parser.parse_args()

    if args.ignorefiles and not os.path.isfile(args.ignorefiles):
        raise SystemExit(f"Ignore file does not exist: {args.ignorefiles}")

    server = build_server(
        args.directory,
        host=args.host,
        port=args.port,
        token=args.token,
        ignorefiles=args.ignorefiles,
    )
    print(
        f"Serving {os.path.abspath(args.directory)} on http://{args.host}:{args.port}/",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
