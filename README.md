# remote-control-helpers

Scripts for exposing a directory over HTTP and mounting it locally as a
read-only filesystem.

- `static_host.py` — stdlib HTTP server (HTML listings, Range, optional token, gitignore hides)
- `fast_host.py` — same behavior as `static_host.py`, FastAPI + **reload always on**
- `http_fs.py` — FUSE client that mounts that HTTP tree

`http_fs.py` caches listings and file sizes in memory. File bodies are fetched on read.

## Install

Minimum (`static_host.py` + `http_fs.py`):

```bash
pip install -r min_requirements.txt
```

Everything, including `fast_host.py`:

```bash
pip install -r requirements.txt
```

`http_fs.py` also needs **libfuse** (Linux or macOS). It does not run on native Windows.

```bash
# Debian/Ubuntu
sudo apt install fuse3

# macOS
brew install macfuse
```

## static_host.py

```bash
python static_host.py /data --port 8888 --host 0.0.0.0 --token secret --ignorefiles .gitignore
```

## fast_host.py

Equivalent to `static_host.py`, but uvicorn **always** starts with `reload=True`.

```bash
python fast_host.py /data --port 8888 --host 0.0.0.0 --token secret --ignorefiles .gitignore
```

CLI flags are the same as `static_host.py`. Code changes under this directory restart the server.

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

Both servers speak Range (`206 Partial Content`, `Accept-Ranges: bytes`).

## http_fs.py

Mount a browsable HTTP directory (Apache/nginx autoindex, `static_host.py`, or `fast_host.py`):

```bash
mkdir -p ~/httpmnt
python http_fs.py http://server:8888/ ~/httpmnt
ls ~/httpmnt
fusermount3 -u ~/httpmnt
```

With a token:

```bash
python http_fs.py http://secret@server:8888/ ~/httpmnt
```

Read-only. Writes are rejected.

On each `read()`, http_fs sends `Range`. If the server returns `206`, only that span is used. If the server ignores Range and returns `200`, http_fs skips the first N bytes on the stream, takes the requested size, and closes the connection. Without Range, a read at offset N still has to skip N bytes on the wire.

Check Range support:

```bash
curl -sI -H "Range: bytes=0-0" http://server:8888/file.txt
```

`206` means Range works. `200` means the fallback skip path.

## Together

Remote (pick one):

```bash
python static_host.py /some/real/data --token secret --ignorefiles .gitignore
# or
python fast_host.py /some/real/data --token secret --ignorefiles .gitignore
```

Local:

```bash
mkdir -p ~/httpmnt
python http_fs.py http://secret@server:8888/ ~/httpmnt
```

Unmount: `fusermount3 -u ~/httpmnt` (Linux) or `umount ~/httpmnt` (macOS).
