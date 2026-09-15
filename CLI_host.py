#!/usr/bin/env python3

import argparse
import asyncio
import base64
import hmac
import html
import os
import urllib.parse

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.websockets import WebSocketState


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


def create_app(token=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket("/")
    async def cli(websocket: WebSocket):
        await websocket.accept()
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
    args = parser.parse_args()
    print(
        f"CLI ws://{args.host}:{args.port}/  "
        f"http://{args.host}:{args.port}/run  "
        f"http://{args.host}:{args.port}/form",
        flush=True,
    )
    uvicorn.run(
        create_app(args.token),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
