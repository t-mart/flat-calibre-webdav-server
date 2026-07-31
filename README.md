# calibre-webdav

A read-only WebDAV server that exposes a Calibre library as a **flat**
collection: every book at the root, no subdirectories. Backed by the real files
on disk; nothing is copied or duplicated.

```
on disk:
/path/to/calibre/library/
  metadata.db
  Frank Herbert/
    Dune (54)/
      Dune - Frank Herbert.epub
    Dune Messiah (55)/
      Dune Messiah - Frank Herbert.epub
  Roberto Bolaño/
    2666 (1)/
      2666 - Roberto Bolaño.epub

over WebDAV:
/
  Herbert, Frank - Dune 01 - Dune.epub
  Herbert, Frank - Dune 02 - Dune Messiah.epub
  Bolaño, Roberto & Wimmer, Natasha - 2666 01 - 2666.epub
```

The intended client is [foldersync](https://github.com/t-mart/foldersync), a
KOReader plugin that mirrors this collection into a local folder on a Kobo.

Flat is deliberate: KOReader shows cover thumbnails at whatever level you are
browsing. It's the most aesthetically pleasing to see a single page of covers.
If there are too many pages, you can always search.

## How it works

`metadata.db` is the sole source of truth for both file location and naming. The
library tree is never walked for discovery, and author and title are never
parsed out of directory names, because Calibre's on-disk names are lossily
sanitized: `Kernighan Brian W_` is really `Kernighan, Brian W.` and
`Dune_ House Atreides` is really `Dune: House Atreides`.

The index is built once at startup from a single query and cached. It is
invalidated by `metadata.db`'s mtime, so a book ingested by Calibre Web
Automated appears within the debounce window with no restart. Rebuilds are
atomic: a new index is built off to the side and the reference swapped, so a
PROPFIND racing a rebuild sees either the old snapshot or the new one, never a
half-built listing. A failed rebuild logs and leaves the previous index in
place.

The database is opened strictly read-only (`mode=ro`), with a connection timeout
and a bounded retry.

## Naming

```
with series:     {author_sort} - {series} {index} - {title}.{ext}
without series:  {author_sort} - {title}.{ext}
```

The series index is zero-padded to two digits with a trailing `.0` dropped, so
`1.0` becomes `01` and `2.5` becomes `02.5`. That makes a series sort into
reading order lexicographically, which is the point of the series branch given a
flat root.

Names are sanitized for FAT32, which is what the Kobo's internal storage uses.
The characters `: * ? " < > | \ /` become `_` (Calibre's own convention),
trailing dots and spaces are stripped, and names are capped at 200 UTF-16 units
with only the title ever truncated. Non-ASCII is preserved exactly:
`Bolaño, Roberto` survives intact.

Where two books would generate the same name, both get a ` (calibre_id)` suffix
and everything else stays clean. Collisions are detected case-insensitively,
because FAT32 is case-insensitive: two names differing only in case are distinct
over WebDAV but would clobber each other once mirrored.

## Drift

Calibre libraries drift and CWA ingests automatically, so this is handled rather
than treated as an error:

- A book with zero `data` rows is skipped silently.
- A `data` row whose file is absent on disk is skipped with a WARN naming the
  book id and the expected path. If a lower-preference format is present, it is
  used instead.
- A book with no format in the preference order is skipped with a WARN.

None of these are a 500 and none abort the index build. The skip count is
reported at the end of every build so drift is visible rather than mysterious.

## Configuration

Everything is an environment variable.

| Variable                      | Default    | Meaning                                                      |
| ----------------------------- | ---------- | ------------------------------------------------------------ |
| `CW_LIBRARY_ROOT`           | _required_ | Calibre library root, the directory containing `metadata.db` |
| `CW_USERNAME`               | _required_ | HTTP Basic username                                          |
| `CW_PASSWORD`               | _required_ | HTTP Basic password                                          |
| `CW_ALLOW_ANONYMOUS`        | `false`    | Serve without authentication; makes the credentials optional |
| `CW_HOST`                   | `0.0.0.0`  | Bind address                                                 |
| `CW_PORT`                   | `8080`     | Bind port                                                    |
| `CW_FORMAT_PREFERENCE`      | `epub,pdf` | Format preference, most preferred first                      |
| `CW_MAX_FILENAME_LENGTH`    | `200`      | Cap in UTF-16 units, under the FAT32 limit of 255            |
| `CW_SANITIZE_REPLACEMENT`   | `_`        | Replacement for FAT32-illegal characters                     |
| `CW_INDEX_DEBOUNCE_SECONDS` | `5`        | Minimum interval between freshness checks                    |
| `CW_DB_TIMEOUT_SECONDS`     | `5`        | SQLite busy timeout                                          |
| `CW_DB_RETRY_ATTEMPTS`      | `3`        | Retries when a writer holds the database                     |
| `CW_VERBOSE`                | `3`        | 0 quiet, 3 info, 4+ debug with access logs                   |

## Running

```shell
docker build --tag calibre-webdav:latest .

docker run --detach --name calibre-webdav --publish 8080:8080 --restart unless-stopped --user 1000:1000 --volume /my/ebooks:/library:ro --env CW_USERNAME=someuser --env CW_PASSWORD=somepassword calibre-webdav:latest
```

Pushes to `master` publish the image to the Forgejo instance's container
registry (see
[.forgejo/workflows/publish-container.yml](.forgejo/workflows/publish-container.yml)),
so the build step can be skipped in favor of pulling
`<forgejo-host>/<owner>/calibre-webdav:latest`. Each push also
leaves behind an immutable tag of the short commit hash.

Mount the library `:ro` so read-only access is enforced by the filesystem and
not merely in code.

Set `--user` to an id that can read the library, adding `--group-add` if it
needs a supplementary group. Any id works, including one with no `/etc/passwd`
entry; the image defaults to `1000:1000` and never runs as root. If the id
cannot read the library the server refuses to start and names the uid, gid, and
groups it had.

## Security

There is deliberately **no TLS support** in this server. It is designed to run
behind a reverse proxy (Caddy) that terminates TLS and obtains certificates.

Authentication is HTTP Basic, which sends credentials base64-encoded, i.e. in
the clear. Therefore, **this should be run only on trusted networks and/or
behind TLS-terminating reverse proxies.**

The server must be mounted at a host root, not a subpath: it generates absolute
hrefs rooted at `/`.

Only a subset of WebDAV server verbs are implemented, which keeps the server
read-only against the filesystem. `PUT`, `DELETE`, `MKCOL`, `MOVE`, `COPY`,
`LOCK`, `UNLOCK` and `PROPPATCH` all return 405. The database is never written,
never vacuumed, and never write-locked.

## WebDAV surface

Only what the client actually uses is implemented:

- `OPTIONS` advertising `DAV: 1`
- `PROPFIND` at `Depth: 0` and `Depth: 1` (the tree is one level deep, so
  `infinity` is the same as `1` and is accepted)
- `GET` and `HEAD`, with `Range` and `Accept-Ranges`

Properties on files: `resourcetype` (empty), `getcontentlength`,
`getlastmodified`, `getetag`, `getcontenttype`, `displayname`, `creationdate`.
`allprop`, `propname` and named `prop` requests are all handled, and unknown
properties come back in a 404 propstat.

`getetag` is derived from the real file on disk (an `mtime-size` digest), never
from database fields, and PROPFIND and GET are locked to the same value. That
matters: the etag is the client's change-detection key, so a listing that
disagreed with the download would silently break syncing.

## Development

```shell
uv sync
uv run pytest
uv run ruff format src tests; uv run ruff check src tests; uv run ty check src tests
```

Tests build a small synthetic Calibre library rather than touching the real one,
covering colons in titles, a `.`-terminated author, a non-ASCII author, a book
with zero `data` rows, a `data` row whose file is missing, colliding names,
a >200-character title, a `.5` series index, and an author with both series and
standalone books.

To exercise it against a real client:

```nu
$env.RCLONE_CONFIG_FLAT_TYPE = "webdav"
$env.RCLONE_CONFIG_FLAT_URL = "http://localhost:8080"
$env.RCLONE_CONFIG_FLAT_VENDOR = "other"
$env.RCLONE_CONFIG_FLAT_USER = "user"
$env.RCLONE_CONFIG_FLAT_PASS = (rclone obscure pass)

rclone lsjson flat:
rclone sync flat: /tmp/mirror --dry-run
```

`rclone lsjson` surfaces exactly the fields the KOReader plugin consumes, and
`rclone sync --dry-run` exercises the same mirror semantics the plugin
implements. Two dry-runs in a row should plan no changes the second time.

## Companion project

The client is a separate KOReader plugin called
[foldersync](https://github.com/t-mart/foldersync). The shared contract is
exactly two things: the flat naming scheme, and `getetag` as the
change-detection key. Everything else on either side can change independently.
