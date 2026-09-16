#!/usr/bin/env python3

import argparse
import asyncio
import base64
import hmac
import html
import ipaddress
import os
import urllib.parse
import warnings

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.websockets import WebSocketState


def _normalize_ip(value):
    text = (value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    addr = ipaddress.ip_address(text)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return str(addr)


def _load_ip_file(path):
    ips = set()
    with open(path, encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                ips.add(_normalize_ip(line))
            except ValueError as exc:
                raise SystemExit(
                    f"{path}:{lineno}: invalid IP {line!r}: {exc}"
                )
    return ips


def _load_ip_lists(whitelist_path, blacklist_path):
    whitelist = None
    if whitelist_path is not None:
        whitelist = _load_ip_file(whitelist_path)
    blacklist = set()
    if blacklist_path is not None:
        blacklist = _load_ip_file(blacklist_path)
    if whitelist is not None:
        overlap = sorted(whitelist & blacklist)
        if overlap:
            message = (
                "IPs in both --whitelist and --blacklist; "
                "blacklist takes precedence: " + ", ".join(overlap)
            )
            warnings.warn(message, stacklevel=2)
            print("Warning:", message, flush=True)
    return whitelist, blacklist


def _peer_ip(host):
    if not host:
        return None
    try:
        return _normalize_ip(host)
    except ValueError:
        return host.strip()


def _ip_allowed(ip, whitelist, blacklist):
    if ip is None:
        return False
    if ip in blacklist:
        return False
    if whitelist is not None:
        return ip in whitelist
    return True


def _forbidden():
    return Response(
        content=b"Forbidden\n",
        status_code=403,
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )


def _tokens_match(got, expected):
    if not got:
        return False
    try:
        return hmac.compare_digest(got, expected)
    except (TypeError, ValueError):
        return False


def _authorized(query_params, headers, token, extra_token=None):
    if token is None:
        return True
    if _tokens_match(extra_token, token):
        return True
    if _tokens_match(query_params.get("token"), token):
        return True
    if _tokens_match(headers.get("x-token"), token):
        return True
    auth = headers.get("authorization") or ""
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


def _parse_form(body):
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {key: (values[-1] if values else "") for key, values in parsed.items()}


def _form_page(cmd="", output="", token_value="", exit_code=None):
    shown = ""
    if output or exit_code is not None:
        shown = (
            f"<p>exit {html.escape(str(exit_code))}</p>"
            f"<pre>{html.escape(output)}</pre>"
        )
    token_input = ""
    if token_value:
        token_input = (
            f'<input type="hidden" name="token" value="{html.escape(token_value, quote=True)}">'
        )
    else:
        token_input = (
            '<label>token <input type="password" name="token"></label><br>'
        )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>CLI_host</title></head><body>"
        "<form method='post' action='/form'>"
        f"{token_input}"
        "<textarea name='cmd' rows='8' cols='80'>"
        f"{html.escape(cmd)}</textarea><br>"
        "<button type='submit'>Run</button>"
        "</form>"
        f"{shown}"
        "</body></html>"
    )


def _unauthorized():
    return Response(
        content=b"Unauthorized\n",
        status_code=401,
        headers={
            "WWW-Authenticate": 'Basic realm="CLI_host"',
            "Content-Type": "text/plain; charset=utf-8",
        },
    )


async def _run_command(command):
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    return text, proc.returncode


async def _stream_command(websocket, command):
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            await websocket.send_text(chunk.decode("utf-8", errors="replace"))
        return await proc.wait()
    except WebSocketDisconnect:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise


def create_app(token=None, whitelist=None, blacklist=None):
    if blacklist is None:
        blacklist = set()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def _request_ip(request):
        host = request.client.host if request.client else None
        return _peer_ip(host)

    @app.middleware("http")
    async def ip_gate(request: Request, call_next):
        if not _ip_allowed(_request_ip(request), whitelist, blacklist):
            return _forbidden()
        return await call_next(request)

    @app.websocket("/")
    async def cli(websocket: WebSocket):
        await websocket.accept()
        ws_ip = _peer_ip(websocket.client.host if websocket.client else None)
        if not _ip_allowed(ws_ip, whitelist, blacklist):
            await websocket.close(code=1008)
            return
        if not _authorized(websocket.query_params, websocket.headers, token):
            await websocket.close(code=1008)
            return
        try:
            command = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        if not command:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
            return
        try:
            await _stream_command(websocket, command)
        except WebSocketDisconnect:
            return
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()

    async def _http_run(request: Request, command: str):
        if not _authorized(request.query_params, request.headers, token):
            return _unauthorized()
        if not command:
            return PlainTextResponse("missing command\n", status_code=400)
        text, code = await _run_command(command)
        return PlainTextResponse(
            text,
            headers={"X-Exit-Code": str(code if code is not None else "")},
        )

    @app.get("/ips")
    async def list_ips(request: Request):
        if not _authorized(request.query_params, request.headers, token):
            return _unauthorized()
        lines = ["whitelist:"]
        if whitelist is None:
            lines.append("(none)")
        elif not whitelist:
            lines.append("(empty)")
        else:
            lines.extend(sorted(whitelist))
        lines.append("blacklist:")
        if not blacklist:
            lines.append("(empty)")
        else:
            lines.extend(sorted(blacklist))
        lines.append("")
        return PlainTextResponse("\n".join(lines))

    @app.get("/run")
    async def run_get(request: Request):
        return await _http_run(request, request.query_params.get("cmd") or "")

    @app.post("/run")
    async def run_post(request: Request):
        body = (await request.body()).decode("utf-8", errors="replace")
        command = body if body else (request.query_params.get("cmd") or "")
        return await _http_run(request, command)

    @app.get("/form")
    async def form_get(request: Request):
        if not _authorized(request.query_params, request.headers, token):
            return _unauthorized()
        return HTMLResponse(
            _form_page(token_value=request.query_params.get("token") or "")
        )

    @app.post("/form")
    async def form_post(request: Request):
        body = (await request.body()).decode("utf-8", errors="replace")
        fields = _parse_form(body)
        command = fields.get("cmd") or ""
        form_token = fields.get("token") or ""
        if not _authorized(
            request.query_params, request.headers, token, extra_token=form_token
        ):
            return _unauthorized()
        if not command:
            return HTMLResponse(
                _form_page(token_value=form_token or request.query_params.get("token") or ""),
                status_code=400,
            )
        text, code = await _run_command(command)
        return HTMLResponse(
            _form_page(
                cmd=command,
                output=text,
                token_value=form_token or request.query_params.get("token") or "",
                exit_code=code,
            ),
            headers={"X-Exit-Code": str(code if code is not None else "")},
        )

    return app


def main():
    parser = argparse.ArgumentParser(
        description="Run a host command over WebSocket, HTTP /run, or HTML /form.",
    )
    parser.add_argument("--port", type=int, default=8888, help="(default: 8888)")
    parser.add_argument("--host", default="0.0.0.0", help="(default: 0.0.0.0)")
    parser.add_argument(
        "--token",
        default=None,
        help="Shared secret via Basic user/password, Bearer, X-Token, or ?token=",
    )
    parser.add_argument(
        "--whitelist",
        default=None,
        help="file of allowed IPs, one per line; if set, only these IPs may connect",
    )
    parser.add_argument(
        "--blacklist",
        default=None,
        help="file of denied IPs, one per line; takes precedence over --whitelist",
    )
    args = parser.parse_args()
    if args.whitelist and not os.path.isfile(args.whitelist):
        raise SystemExit(f"Whitelist file does not exist: {args.whitelist}")
    if args.blacklist and not os.path.isfile(args.blacklist):
        raise SystemExit(f"Blacklist file does not exist: {args.blacklist}")
    whitelist, blacklist = _load_ip_lists(args.whitelist, args.blacklist)
    print(
        f"CLI ws://{args.host}:{args.port}/  "
        f"http://{args.host}:{args.port}/run  "
        f"http://{args.host}:{args.port}/form  "
        f"http://{args.host}:{args.port}/ips",
        flush=True,
    )
    uvicorn.run(
        create_app(args.token, whitelist=whitelist, blacklist=blacklist),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
