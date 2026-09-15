# remote-control-helpers

Two scripts for exposing a directory over HTTP and mounting it locally as a
read-only filesystem.

- `static_host.py` — HTTP server with HTML listings, Range reads, optional token, gitignore-style hides
- `httpfs.py` — FUSE client that mounts that HTTP tree

Listings and file sizes are cached in memory. File bodies are fetched on read.

## Install

```bash
pip install -r requirements.txt
```

`httpfs.py` also needs **libfuse** (Linux or macOS). It does not run on native Windows.

```bash
# Debian/Ubuntu
sudo apt install fuse3

# macOS
brew install macfuse
```

## static_host.py

Serve a directory (HTML index, HTTP Range, optional auth):

```bash
python static_host.py /data --port 8888 --host 0.0.0.0 --token secret --ignorefiles .gitignore
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

The server speaks Range (`206 Partial Content`, `Accept-Ranges: bytes`).

## httpfs.py

Mount a browsable HTTP directory (Apache/nginx autoindex, Python `http.server`, or `static_host.py`):

```bash
mkdir -p ~/httpmnt
python httpfs.py http://server:8888/ ~/httpmnt
ls ~/httpmnt
fusermount3 -u ~/httpmnt
```

With a token from `static_host.py`:

```bash
python httpfs.py http://secret@server:8888/ ~/httpmnt
```

Read-only. Writes are rejected.

On each `read()`, httpfs sends `Range`. If the server returns `206`, only that span is used. If the server ignores Range and returns `200`, httpfs skips the first N bytes on the stream, takes the requested size, and closes the connection. Without Range, a read at offset N still has to skip N bytes on the wire.

Check Range support:

```bash
curl -sI -H "Range: bytes=0-0" http://server:8888/file.txt
```

`206` means Range works. `200` means the fallback skip path.

## Together

Remote:

```bash
python static_host.py /some/real/data --token secret --ignorefiles .gitignore
```

Local:

```bash
mkdir -p ~/httpmnt
python httpfs.py http://secret@server:8888/ ~/httpmnt
```

Unmount: `fusermount3 -u ~/httpmnt` (Linux) or `umount ~/httpmnt` (macOS).
