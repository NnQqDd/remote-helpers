# remote-control-helpers

Scripts for exposing a directory over HTTP, mounting it locally as a
read-only filesystem, and running a remote shell over WebSocket.

- `static_host.py` — stdlib HTTP server (HTML listings, Range, optional token, gitignore hides)
- `fast_host.py` — same as `static_host.py`, FastAPI + **reload always on**
- `http_fs.py` — FUSE client that mounts that HTTP tree
- `CLI_host.py` — WebSocket `/`, HTTP `/run`, HTML form `/form`

## Install

Minimum (`static_host.py` + `http_fs.py`):

```bash
pip install -r min_requirements.txt
```

Everything (`fast_host.py`, `CLI_host.py`, and the example WebSocket client):

```bash
pip install -r requirements.txt
```

`http_fs.py` also needs **libfuse** (Linux or macOS). Native Windows cannot mount.

```bash
# Debian/Ubuntu
sudo apt install fuse3

# macOS
brew install macfuse
```

## What runs where

Tested on Windows and Ubuntu 24.04 WSL2:

| | Windows | Ubuntu / macOS |
|---|---|---|
| `static_host.py` / `fast_host.py` | yes | yes |
| `CLI_host.py` | yes | yes |
| HTTP/WebSocket **client** | yes | yes |
| `http_fs.py` FUSE **mount** | no | yes |

A Windows machine can **serve** files or a shell. A FUSE mount of that server has to run on Ubuntu (or macOS), including WSL.

`http_fs.py` sends URL userinfo (`http://secret@host:8888/`) as HTTP Basic. urllib treats `secret@host` as a hostname if you leave it in the URL.

Commands sent to `CLI_host.py` run **on the server OS** (`python` vs `python3`, `dir` vs `ls`).

Use different `--port` values if you run more than one helper at once.

## static_host.py

```bash
python static_host.py /data --port 8888 --host 0.0.0.0 --token secret --ignorefiles .gitignore
```

## fast_host.py

Same flags as `static_host.py`. uvicorn always starts with `reload=True`.

```bash
python fast_host.py /data --port 8888 --host 0.0.0.0 --token secret --ignorefiles .gitignore
```

| Flag | Default | Meaning |
|---|---|---|
| `directory` | `.` | Directory to serve |
| `--port` | `8888` | Listen port |
| `--host` | `0.0.0.0` | Bind address |
| `--token` | none | Shared secret; every request must send it |
| `--ignorefiles` | none | gitignore file of paths to hide from listings and GET |

`--ignorefiles` uses [gitignore_parser](https://github.com/mherrmann/gitignore_parser) (`.gitignore` syntax: `*.log`, `/secret.txt`, `build/`, `!keep.log`, …). Matching names are omitted from listings and return 404.

If `--token` is set, send it as one of:

- HTTP Basic user or password: `http://secret@host:8888/`
- `Authorization: Bearer secret`
- `X-Token: secret`
- `?token=secret`

Both file servers speak Range (`206 Partial Content`, `Accept-Ranges: bytes`).

## http_fs.py

Mount a browsable HTTP directory (Apache/nginx autoindex, `static_host.py`, or `fast_host.py`):

```bash
mkdir -p ~/httpmnt
python http_fs.py http://secret@server:8888/ ~/httpmnt
ls ~/httpmnt
fusermount3 -u ~/httpmnt
```

Read-only. Writes are rejected.

On each `read()`, http_fs sends `Range`. If the server returns `206`, only that span is used. If the server ignores Range and returns `200`, http_fs skips the first N bytes on the wire, takes the requested size, and closes the connection.

```bash
curl -sI -H "Range: bytes=0-0" http://server:8888/file.txt
```

`206` means Range works. `200` means the skip path.

## Together

Remote (pick one):

```bash
python static_host.py /some/real/data --token secret --ignorefiles .gitignore
# or
python fast_host.py /some/real/data --token secret --ignorefiles .gitignore
```

Local (Linux/macOS/WSL):

```bash
mkdir -p ~/httpmnt
python http_fs.py http://secret@server:8888/ ~/httpmnt
```

Unmount: `fusermount3 -u ~/httpmnt` (Linux) or `umount ~/httpmnt` (macOS).

## CLI_host.py

Four APIs. Commands run on the **server OS**.

| API | Path | Input | Output |
|---|---|---|---|
| WebSocket | `ws://host:8888/` | one text frame = command | streamed stdout/stderr text frames; socket closes when done |
| HTTP | `http://host:8888/run` | GET `cmd=` or POST raw body | `text/plain` full output; `X-Exit-Code` header |
| Form | `http://host:8888/form` | HTML form or POST `cmd=` | HTML page with the output |
| IPs | `http://host:8888/ips` | none | `text/plain` whitelist then blacklist |

```bash
python CLI_host.py --port 8888 --host 0.0.0.0 --token secret \
  --whitelist allow.txt --blacklist deny.txt
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | `8888` | Listen port |
| `--host` | `0.0.0.0` | Bind address |
| `--token` | none | `?token=`, Bearer, Basic, `X-Token`, or form field `token` |
| `--whitelist` | none | file, one IP per line; if set, only these IPs may use the APIs |
| `--blacklist` | none | file, one IP per line; always denied. If an IP is in both files, a warning is printed and **blacklist wins** |

### IPs `/ips`

```bash
curl "http://127.0.0.1:8888/ips?token=secret"
```

### HTTP `/run` (curl)

```bash
curl "http://127.0.0.1:8888/run?token=secret&cmd=echo+hello"

curl --data-binary "echo hello" "http://127.0.0.1:8888/run?token=secret"

curl --data-binary "echo hello" -H "X-Token: secret" http://127.0.0.1:8888/run

curl -u secret: --data-binary "echo hello" http://127.0.0.1:8888/run
```

### Form `/form` (browser or curl)

Open in a browser:

```text
http://127.0.0.1:8888/form?token=secret
```

```bash
curl -d "cmd=echo hello" "http://127.0.0.1:8888/form?token=secret"

curl -d "cmd=echo hello" -d "token=secret" http://127.0.0.1:8888/form
```

### WebSocket `/`

```bash
python -c "
import asyncio, websockets
async def main():
    uri = 'ws://127.0.0.1:8888/?token=secret'
    async with websockets.connect(uri) as ws:
        await ws.send('echo hello')
        async for msg in ws:
            print(msg, end='')
asyncio.run(main())
"
```


