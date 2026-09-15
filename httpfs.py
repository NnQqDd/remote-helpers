#!/usr/bin/env python3

import argparse
import errno
import html.parser
import os
import posixpath
import stat
import urllib.error
import urllib.parse
import urllib.request

from fuse import FUSE, Operations


class DirectoryParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        attrs = dict(attrs)
        href = attrs.get("href")

        if href:
            self.links.append(href)


class HTTPFileSystem(Operations):
    def __init__(self, base_url):
        if not base_url.endswith("/"):
            base_url += "/"

        self.base_url = base_url
        self.dir_cache = {}
        self.size_cache = {}

    def _http_url(self, path):
        relative = path.lstrip("/")
        return urllib.parse.urljoin(
            self.base_url,
            urllib.parse.quote(relative, safe="/"),
        )

    def _dir_url(self, path):
        url = self._http_url(path)
        if not url.endswith("/"):
            url += "/"
        return url

    def _list_dir(self, path):
        if path in self.dir_cache:
            return self.dir_cache[path]

        url = self._dir_url(path)

        try:
            with urllib.request.urlopen(url) as response:
                body = response.read()
                current_url = response.url
        except Exception as exc:
            raise OSError(
                errno.EIO,
                f"HTTP directory listing failed for {url}: {exc}",
            )

        if not current_url.endswith("/"):
            current_url += "/"

        parser = DirectoryParser()
        parser.feed(body.decode("utf-8", errors="replace"))

        entries = {}

        for href in parser.links:
            if not href:
                continue

            if href.startswith("#") or href.startswith("?"):
                continue

            absolute_url = urllib.parse.urljoin(current_url, href)
            parsed = urllib.parse.urlparse(absolute_url)

            if parsed.scheme not in ("http", "https"):
                continue

            clean_path = urllib.parse.unquote(parsed.path)
            base_parsed = urllib.parse.urlparse(self.base_url)
            base_path = urllib.parse.unquote(base_parsed.path)

            if not clean_path.startswith(base_path):
                continue

            relative = clean_path[len(base_path):].lstrip("/")
            was_directory = parsed.path.endswith("/")
            relative = posixpath.normpath(relative)

            if relative in ("", ".", ".."):
                continue

            if relative.startswith("../"):
                continue

            current = path.strip("/")

            if current:
                prefix = current.rstrip("/") + "/"
                if not relative.startswith(prefix):
                    continue
                child = relative[len(prefix):]
            else:
                child = relative

            if not child or "/" in child:
                continue

            entries[child] = {
                "type": "dir" if was_directory else "file"
            }

        self.dir_cache[path] = entries
        return entries

    def _entry(self, path):
        if path == "/":
            return {"type": "dir"}

        clean_path = path.rstrip("/")
        parent = posixpath.dirname(clean_path)
        if not parent:
            parent = "/"

        name = posixpath.basename(clean_path)
        entries = self._list_dir(parent)
        entry = entries.get(name)

        if entry is None:
            raise OSError(
                errno.ENOENT,
                "No such file or directory",
            )

        return entry

    def getattr(self, path, fh=None):
        if path == "/":
            return {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_nlink": 2,
            }

        entry = self._entry(path)

        if entry["type"] == "dir":
            return {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_nlink": 2,
            }

        size = self._get_size(path)

        return {
            "st_mode": stat.S_IFREG | 0o444,
            "st_nlink": 1,
            "st_size": size,
        }

    def readdir(self, path, fh):
        entries = self._list_dir(path)
        yield "."
        yield ".."
        for name in sorted(entries):
            yield name

    def open(self, path, flags):
        entry = self._entry(path)

        if entry["type"] != "file":
            raise OSError(
                errno.EISDIR,
                "Is a directory",
            )

        if flags & (os.O_WRONLY | os.O_RDWR):
            raise OSError(
                errno.EROFS,
                "Read-only filesystem",
            )

        return 0

    def read(self, path, size, offset, fh):
        entry = self._entry(path)

        if entry["type"] != "file":
            raise OSError(
                errno.EISDIR,
                "Is a directory",
            )

        if size <= 0:
            return b""

        url = self._http_url(path)
        request = urllib.request.Request(url)
        request.add_header(
            "Range",
            f"bytes={offset}-{offset + size - 1}",
        )

        try:
            with urllib.request.urlopen(request) as response:
                if response.status == 206:
                    return response.read()
                return self._read_from_start(response, offset, size)

        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                return b""
            raise OSError(
                errno.EIO,
                f"HTTP read failed for {url}: {exc}",
            )

        except Exception as exc:
            raise OSError(
                errno.EIO,
                f"HTTP read failed for {url}: {exc}",
            )

    def _read_from_start(self, response, offset, size):
        remaining_skip = offset
        while remaining_skip > 0:
            chunk = response.read(min(remaining_skip, 1024 * 1024))
            if not chunk:
                return b""
            remaining_skip -= len(chunk)

        parts = []
        remaining = size
        while remaining > 0:
            chunk = response.read(remaining)
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)

        return b"".join(parts)

    def _get_size(self, path):
        if path in self.size_cache:
            return self.size_cache[path]

        url = self._http_url(path)

        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request) as response:
                content_length = response.headers.get("Content-Length")

            if content_length is not None:
                size = int(content_length)
                self.size_cache[path] = size
                return size

        except Exception:
            pass

        try:
            with urllib.request.urlopen(url) as response:
                data = response.read()

            size = len(data)
            self.size_cache[path] = size
            return size

        except Exception as exc:
            raise OSError(
                errno.EIO,
                f"Could not determine file size for {url}: {exc}",
            )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Mount a browsable HTTP directory as a "
            "read-only FUSE filesystem."
        )
    )
    parser.add_argument(
        "url",
        help="HTTP/HTTPS directory URL, e.g. http://server/data/",
    )
    parser.add_argument(
        "mountpoint",
        help="Local FUSE mountpoint",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.mountpoint):
        raise SystemExit(
            f"Mountpoint does not exist: {args.mountpoint}"
        )

    FUSE(
        HTTPFileSystem(args.url),
        args.mountpoint,
        foreground=True,
        ro=True,
        allow_other=False,
    )


if __name__ == "__main__":
    main()
