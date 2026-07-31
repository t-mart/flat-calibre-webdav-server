# calibre-webdav

A read-only WebDAV server that exposes a Calibre library in whatever layout you
describe with a path template. Backed by the real files on disk; nothing is
copied or duplicated.

```
on disk:
/path/to/calibre/library/
  metadata.db
  Frank Herbert/
    Dune (54)/
      Dune - Frank Herbert.epub
    Dune Messiah (55)/
      Dune Messiah - Frank Herbert.epub
  Roberto Bolaño & Natasha Wimmer/
    2666 (1)/
      2666 - Roberto Bolaño.epub

over WebDAV, with the default template:
/
  Herbert, Frank/
    Dune/
      01 - Dune.epub
      02 - Dune Messiah.epub
  Bolaño, Roberto & Wimmer, Natasha/
    2666.epub
```

The intended client is [foldersync](https://github.com/t-mart/foldersync), a
KOReader plugin that mirrors this collection into a local folder on a Kobo.

## The path template

`CW_PATH_TEMPLATE` decides the whole shape of what is served. `/` separates
path components, so the depth of the tree is yours to choose: a template with no
`/` in it puts every book at the root.

```
{field}                  the field's value
{field:|prefix|suffix}   prefix + value + suffix, or nothing at all when the
                         field is empty
```

The second form is what makes one template cover books with and without a
series. `{series:||/}` renders `Dune/` for a book in a series and nothing for a
standalone one, so the series directory simply does not exist for books that
have no series. The syntax is Calibre's own save-template shape.

### Fields

| Field            | From                | Notes                                          |
| ---------------- | ------------------- | ---------------------------------------------- |
| `{author_sort}`  | `books.author_sort` | `Herbert, Brian & Anderson, Kevin J.`          |
| `{title}`        | `books.title`       |                                                |
| `{title_sort}`   | `books.sort`        | `Hobbit, The`; falls back to the title         |
| `{series}`       | `series.name`       | Empty for a standalone book                    |
| `{series_index}` | `books.series_index`| `01`, `02.5`; empty without a series           |
| `{year}`         | `books.pubdate`     | Empty when the book has no publication date    |
| `{id}`           | `books.id`          | Calibre's own id, unique by construction       |
| `{ext}`          | `data.format`       | Lowercased. Required: a template without it is refused |

### Examples

Two books: *Dune Messiah* (Frank Herbert, book 2 of Dune, 1969) and *2666*
(Roberto Bolaño, with the translator Natasha Wimmer as a second author, no
series, 2004).

| Template                                                                    | Dune Messiah                                   | 2666                                                 |
| --------------------------------------------------------------------------- | ---------------------------------------------- | ---------------------------------------------------- |
| `{author_sort}/{series:\|\|/}{series_index:\|\| - }{title}.{ext}` (default) | `Herbert, Frank/Dune/02 - Dune Messiah.epub`   | `Bolaño, Roberto & Wimmer, Natasha/2666.epub`        |
| `{author_sort} - {series:\|\| }{series_index:\|\| - }{title}.{ext}` (flat)  | `Herbert, Frank - Dune 02 - Dune Messiah.epub` | `Bolaño, Roberto & Wimmer, Natasha - 2666.epub`      |
| `{author_sort}/{title}.{ext}`                                               | `Herbert, Frank/Dune Messiah.epub`             | `Bolaño, Roberto & Wimmer, Natasha/2666.epub`        |
| `{year:\|\|/}{title} - {author_sort}.{ext}`                                 | `1969/Dune Messiah - Herbert, Frank.epub`      | `2004/2666 - Bolaño, Roberto & Wimmer, Natasha.epub` |
| `{title_sort}.{ext}`                                                        | `Dune Messiah.epub`                            | `2666.epub`                                          |

The series index is zero-padded to two digits with a trailing `.0` dropped, so
`1.0` becomes `01` and `2.5` becomes `02.5`. That makes a series sort into
reading order lexicographically.

A flat template is worth considering for KOReader specifically: it shows cover
thumbnails at whatever level you are browsing, so a single directory means a
single page of covers to scroll. If that gets too long, search still works.

Templates are parsed and checked at startup. An unknown field, a missing
`{ext}`, an unbalanced brace, a `..` component, or a character the target
filesystem cannot take are all startup errors rather than surprises later.

## Naming

Field values are sanitized before they are substituted into the template, which
is what stops a title like `AC/DC` from inventing a path component. Two sets of
characters are involved:

- `/` and control characters are always replaced with `_`. This is not a
  nicety; a `/` inside a value would split one component into two.
- `: * ? " < > | \` are replaced only when the target is FAT32, which is what
  the Kobo's internal storage uses. `CW_FAT32_REPLACEMENT` names the
  replacement (`_` by default, Calibre's own convention, so `Dune: House
  Atreides` becomes `Dune_ House Atreides`). Setting it to `false` turns this
  off, along with the stripping of trailing dots and spaces that FAT32 also
  requires. Note that leaving it on costs you the dot in `Salinger, J. D.`,
  since FAT32 rejects a component ending in one.

Non-ASCII is preserved exactly either way: `Bolaño, Roberto` survives intact.

Each path component is capped independently at `CW_MAX_FILENAME_LENGTH` UTF-16
units. On the last one the extension and any id suffix are preserved and only
the stem is truncated.

Where two books would generate the same path, both get a ` (calibre_id)` suffix
before the extension and everything else stays clean. Collisions are detected
case-insensitively, because the destination filesystem may be: two names
differing only in case are distinct over WebDAV but would clobber each other
once mirrored.

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

The directory tree is derived from the rendered paths and held in memory
alongside them, so a listing at any depth costs no filesystem work beyond
stat'ing the books themselves. Requests resolve against that index and never
against the filesystem, which is what makes path traversal a non-question.

The database is opened strictly read-only (`mode=ro`), with a connection timeout
and a bounded retry.

## Drift

Calibre libraries drift and CWA ingests automatically, so this is handled rather
than treated as an error:

- A book with zero `data` rows is skipped silently.
- A `data` row whose file is absent on disk is skipped with a WARN naming the
  book id and the expected path. If a lower-preference format is present, it is
  used instead.
- A book with no format in the preference order is skipped with a WARN.
- A book whose path is also a directory is skipped with a WARN. The directory
  wins, since dropping it would take its contents with it.

None of these are a 500 and none abort the index build. The skip count is
reported at the end of every build so drift is visible rather than mysterious.

## Configuration

Everything is an environment variable.

| Variable                      | Default                                                      | Meaning                                                      |
| ----------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| `CW_LIBRARY_ROOT`             | _required_                                                   | Calibre library root, the directory containing `metadata.db` |
| `CW_USERNAME`                 | _required_                                                   | HTTP Basic username                                          |
| `CW_PASSWORD`                 | _required_                                                   | HTTP Basic password                                          |
| `CW_ALLOW_ANONYMOUS`          | `false`                                                      | Serve without authentication; makes the credentials optional |
| `CW_HOST`                     | `0.0.0.0`                                                    | Bind address                                                 |
| `CW_PORT`                     | `8080`                                                       | Bind port                                                    |
| `CW_PATH_TEMPLATE`            | `{author_sort}/{series:\|\|/}{series_index:\|\| - }{title}.{ext}` | The served layout                                        |
| `CW_FORMAT_PREFERENCE`        | `epub,pdf`                                                   | Format preference, most preferred first                      |
| `CW_MAX_FILENAME_LENGTH`      | `200`                                                        | Per-component cap in UTF-16 units, under the FAT32 limit of 255 |
| `CW_FAT32_REPLACEMENT`        | `_`                                                          | Replacement for FAT32-illegal characters, or `false` to leave them alone |
| `CW_INDEX_DEBOUNCE_SECONDS`   | `5`                                                          | Minimum interval between freshness checks                    |
| `CW_DB_TIMEOUT_SECONDS`       | `5`                                                          | SQLite busy timeout                                          |
| `CW_DB_RETRY_ATTEMPTS`        | `3`                                                          | Retries when a writer holds the database                     |
| `CW_VERBOSE`                  | `3`                                                          | 0 quiet, 3 info, 4+ debug with access logs                   |

## Running

```shell
docker build --tag calibre-webdav:latest .

docker run --detach --name calibre-webdav --publish 8080:8080 --restart unless-stopped --user 1000:1000 --volume /my/ebooks:/library:ro --env CW_USERNAME=someuser --env CW_PASSWORD=somepassword calibre-webdav:latest
```

Pushes to `master` publish the image to the Forgejo instance's container
registry (see
[.forgejo/workflows/publish-docker-image.yml](.forgejo/workflows/publish-docker-image.yml)),
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

Only what a mirroring client actually uses is implemented:

- `OPTIONS` advertising `DAV: 1`
- `PROPFIND` at `Depth: 0`, `Depth: 1` and `Depth: infinity`. Infinity is
  answered rather than refused, because the tree is already in memory; an
  absent `Depth` header means infinity, per RFC 4918.
- `GET` and `HEAD`, with `Range` and `Accept-Ranges`

Properties on files: `resourcetype` (empty), `getcontentlength`,
`getlastmodified`, `getetag`, `getcontenttype`, `displayname`, `creationdate`.
Collections carry `resourcetype`, `displayname` and `getlastmodified` only; they
exist in the index rather than on disk, so their last-modified is
`metadata.db`'s. `allprop`, `propname` and named `prop` requests are all
handled, and unknown properties come back in a 404 propstat.

`getetag` is derived from the real file on disk (an `mtime-size` digest), never
from database fields, and PROPFIND and GET are locked to the same value. No
client is required to use it, but one that does will not be lied to.

## Development

```shell
uv sync
uv run pytest
uv run ruff format src tests; uv run ruff check src tests; uv run ty check src tests
```

Tests build a small synthetic Calibre library rather than touching the real one,
covering colons in titles, a `.`-terminated author, a non-ASCII author, a book
with zero `data` rows, a `data` row whose file is missing, colliding paths, a
book that collides with a directory, a >200-character title, a `.5` series
index, an author with both series and standalone books, and the flat template
alongside the default one.

To exercise it against a real client:

```nu
$env.RCLONE_CONFIG_CW_TYPE = "webdav"
$env.RCLONE_CONFIG_CW_URL = "http://localhost:8080"
$env.RCLONE_CONFIG_CW_VENDOR = "other"
$env.RCLONE_CONFIG_CW_USER = "user"
$env.RCLONE_CONFIG_CW_PASS = (rclone obscure pass)

rclone lsf cw: --recursive
rclone sync cw: /tmp/mirror --dry-run
```

`rclone sync --dry-run` exercises the same mirror semantics a syncing client
implements. Two dry-runs in a row should plan no changes the second time.

## Companion project

The client is a separate KOReader plugin called
[foldersync](https://github.com/t-mart/foldersync). It mirrors whatever tree it
is pointed at, so there is no shared naming contract to keep in step: change the
template here and the plugin follows without knowing anything changed.
