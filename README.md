# calibre-webdav

A read-only WebDAV server. It exposes a Calibre library in the layout that you
describe with a path template. The server reads the real files on disk. It does
not copy them and does not duplicate them.

The intended client is [foldersync](https://github.com/t-mart/foldersync), a
KOReader plugin. Other clients may also find use though.

## The path template

`CW_PATH_TEMPLATE` sets the shape of the served tree. A `/` character separates
the path components, and you select the depth of the tree. A template without a
`/` character puts every book at the root.

```
{field}                  the value of the field
{field:|prefix|suffix}   prefix + value + suffix, or nothing when the field is
                         empty
```

The second form covers books with a series and books without a series in one
template. For a book in a series, `{series:||/}` renders `Dune/`. For a
standalone book, it renders nothing, and the series directory does not exist.
This syntax is the syntax of the Calibre save template.

### Fields

| Field            | From                 | Notes                                               |
| ---------------- | -------------------- | --------------------------------------------------- |
| `{author_sort}`  | `books.author_sort`  | For example, `Herbert, Brian & Anderson, Kevin J.`  |
| `{title}`        | `books.title`        |                                                     |
| `{title_sort}`   | `books.sort`         | For example, `Hobbit, The`. Falls back to the title |
| `{series}`       | `series.name`        | Empty for a standalone book                         |
| `{series_index}` | `books.series_index` | For example, `01` or `02.5`. Empty without a series |
| `{year}`         | `books.pubdate`      | Empty when the book has no publication date         |
| `{id}`           | `books.id`           | The Calibre id. It is always unique                 |
| `{ext}`          | `data.format`        | Lowercase. Each template must contain this field    |

### Examples

These examples use two books:

- _Dune Messiah_, by Frank Herbert, book 2 of the Dune series, 1969.
- _2666_, by Roberto Bolaño, with the translator Natasha Wimmer as a second
  author, no series, 2004.

| Template                                                                    | Dune Messiah                                   | 2666                                                 |
| --------------------------------------------------------------------------- | ---------------------------------------------- | ---------------------------------------------------- |
| `{author_sort}/{series:\|\|/}{series_index:\|\| - }{title}.{ext}` (default) | `Herbert, Frank/Dune/02 - Dune Messiah.epub`   | `Bolaño, Roberto & Wimmer, Natasha/2666.epub`        |
| `{author_sort} - {series:\|\| }{series_index:\|\| - }{title}.{ext}` (flat)  | `Herbert, Frank - Dune 02 - Dune Messiah.epub` | `Bolaño, Roberto & Wimmer, Natasha - 2666.epub`      |
| `{author_sort}/{title}.{ext}`                                               | `Herbert, Frank/Dune Messiah.epub`             | `Bolaño, Roberto & Wimmer, Natasha/2666.epub`        |
| `{year:\|\|/}{title} - {author_sort}.{ext}`                                 | `1969/Dune Messiah - Herbert, Frank.epub`      | `2004/2666 - Bolaño, Roberto & Wimmer, Natasha.epub` |
| `{title_sort}.{ext}`                                                        | `Dune Messiah.epub`                            | `2666.epub`                                          |

The series index has two digits, with a zero as a prefix. A trailing `.0` is
removed. Thus `1.0` becomes `01`, and `2.5` becomes `02.5`. A series then sorts
into reading order.

The server checks the template at startup. The server refuses to start if the
template has one of these faults:

- an unknown field
- no `{ext}` field
- an unbalanced brace
- a `..` path component
- a character that the target filesystem refuses

## Names

The server sanitizes each field value before it puts the value into the
template. A title such as `AC/DC` therefore cannot create a path component. Two
sets of characters are relevant:

- The server always replaces `/` and the control characters with `_`. This rule
  is necessary, because a `/` character in a value splits one component into two
  components.
- The server replaces `: * ? " < > | \` with `_` when `CW_FAT32` is `true`. This
  rule is necessary because FAT32 refuses these characters.
  `Dune: House Atreides` becomes `Dune_ House Atreides`. To stop this
  replacement, set the variable to `false`. The variable also controls the
  removal of the trailing dots and spaces that FAT32 refuses. If you keep the
  FAT32 replacement, the name `Salinger, J. D.` loses the final dot.

The server caps each path component at `CW_MAX_FILENAME_LENGTH` UTF-16 units. In
the last component, the server keeps the extension and the id suffix, and
truncates only the stem. This also is for FAT filesystems.

If two books get the same path, the server adds a ` (calibre_id)` suffix to both
paths, before the extension. All other paths stay clean. The server compares the
paths without case, because the destination filesystem can be case-insensitive.
Two names that differ only in case are different over WebDAV. On the device,
they replace each other.

## How it works

`metadata.db` is the only source for the location of a file and for its name.
The server never walks the library tree for discovery. The server never reads
the author and the title from the directory names. Calibre sanitizes its own
names on disk, with a loss of information. For example, `Kernighan Brian W_` is
really `Kernighan, Brian W.`, and `Dune_ House Atreides` is really
`Dune: House Atreides`.

The server holds the directory tree in memory together with the paths. A listing
at any depth needs no work on the filesystem, except the stat of each book. All
requests resolve against the index.

## Drift

Calibre libraries drift from the filesystem. The server handles these four
conditions. They are not errors:

- A book has zero `data` rows. The server skips the book silently.
- A `data` row points to a file that is absent from disk. The server skips the
  row and writes a WARN message with the book id and the expected path. If a
  format with a lower preference is present, the server uses that format.
- A book has no format in the preference order. The server skips the book and
  writes a WARN message.
- The path of a book is also a directory. The server skips the book and writes a
  WARN message. The directory stays, because a removal of the directory also
  removes its contents.

No condition in this list causes a 500 response, and no condition stops the
index build. The server reports the number of skipped books at the end of each
build.

## Configuration

Each option is an environment variable.

| Variable                    | Default                                                           | Meaning                                                            |
| --------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------ |
| `CW_LIBRARY_ROOT`           | _required_                                                        | The Calibre library root. This directory contains `metadata.db`    |
| `CW_USERNAME`               | _required_                                                        | The user name for HTTP Basic authentication                        |
| `CW_PASSWORD`               | _required_                                                        | The password for HTTP Basic authentication                         |
| `CW_ALLOW_ANONYMOUS`        | `false`                                                           | Serve without authentication. The credentials become optional      |
| `CW_HOST`                   | `0.0.0.0`                                                         | The bind address                                                   |
| `CW_PORT`                   | `8080`                                                            | The bind port                                                      |
| `CW_PATH_TEMPLATE`          | `{author_sort}/{series:\|\|/}{series_index:\|\| - }{title}.{ext}` | The layout of the served tree                                      |
| `CW_FORMAT_PREFERENCE`      | `epub,pdf`                                                        | The format preference. The first format is the preferred one       |
| `CW_MAX_FILENAME_LENGTH`    | `200`                                                             | The cap for one component, in UTF-16 units. The FAT32 limit is 255 |
| `CW_FAT32`                  | `true`                                                            | Replace the characters that FAT32 refuses with `_`                 |
| `CW_INDEX_DEBOUNCE_SECONDS` | `5`                                                               | The minimum interval between two freshness checks                  |
| `CW_DB_TIMEOUT_SECONDS`     | `5`                                                               | The busy timeout for SQLite                                        |
| `CW_DB_RETRY_ATTEMPTS`      | `3`                                                               | The number of retries when a writer holds the database             |
| `CW_VERBOSE`                | `3`                                                               | 0 is quiet. 3 is info. 4 and more add debug and access logs        |

## How to run the server

```shell
docker build --tag calibre-webdav:latest .

docker run --detach --name calibre-webdav --publish 8080:8080 --restart unless-stopped --user 1000:1000 --volume /my/ebooks:/library:ro --env CW_USERNAME=someuser --env CW_PASSWORD=somepassword calibre-webdav:latest
```

Each push to `master` publishes the image to the container registry of the
Forgejo instance. See
[.forgejo/workflows/publish-docker-image.yml](.forgejo/workflows/publish-docker-image.yml).
You can then pull `<forgejo-host>/<owner>/calibre-webdav:latest`, and the build
step is not necessary. Each push also creates an immutable tag with the short
commit hash.

Mount the library with `:ro`. The filesystem then enforces the read-only access,
and not only the code.

Set `--user` to an id that can read the library. Add `--group-add` for a
supplementary group. Any id works, and the id does not need an entry in
`/etc/passwd`. The image uses `1000:1000` by default and never runs as root.

## Security

**Do not run this server on an untrusted network without TLS.** Authentication
is HTTP Basic. HTTP Basic sends the credentials with base64 encoding, which is
clear text. Run the server behind a reverse proxy such as Caddy.

## WebDAV surface

The server implements only the methods that a mirror client uses:

- `OPTIONS`, which advertises `DAV: 1`
- `PROPFIND` at `Depth: 0`, `Depth: 1` and `Depth: infinity`
- `GET` and `HEAD`, with `Range` and `Accept-Ranges`

The server implements only a subset of the WebDAV methods, and is therefore
read-only against the filesystem. `PUT`, `DELETE`, `MKCOL`, `MOVE`, `COPY`,
`LOCK`, `UNLOCK` and `PROPPATCH` return 405. The server never writes to the
database, never vacuums it, and never takes a write lock.

## Development

```shell
uv sync
uv run pytest
uv run ruff format src tests; uv run ruff check src tests; uv run ty check src tests
```

The tests build a small synthetic Calibre library and never touch the real one.
They cover these cases:

- a colon in a title
- an author name that ends with a dot
- a non-ASCII author name
- a book with zero `data` rows
- a `data` row whose file is absent
- two paths that collide
- a book that collides with a directory
- a title with more than 200 characters
- a `.5` series index
- an author with series books and standalone books
- the flat template and the default template

To test the server against a real client, use rclone:

```nu
$env.RCLONE_CONFIG_CW_TYPE = "webdav"
$env.RCLONE_CONFIG_CW_URL = "http://localhost:8080"
$env.RCLONE_CONFIG_CW_VENDOR = "other"
$env.RCLONE_CONFIG_CW_USER = "user"
$env.RCLONE_CONFIG_CW_PASS = (rclone obscure pass)

rclone lsf cw: --recursive
rclone sync cw: /tmp/mirror --dry-run
```

`rclone sync --dry-run` uses the same mirror semantics as a sync client. Run the
dry run two times. The second run must plan no changes.

## Companion project

The client is a separate KOReader plugin:
[foldersync](https://github.com/t-mart/foldersync). The plugin mirrors the tree
that it receives. There is no shared contract for the names. If you change the
template here, the plugin follows the change and needs no update.
