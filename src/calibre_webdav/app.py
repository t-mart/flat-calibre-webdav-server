"""Starlette application and entry point."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial

import anyio.to_thread
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from .auth import BasicAuthMiddleware
from .config import Config, ConfigError
from .index import IndexCache, LibraryIndex
from .webdav import (
    ALLOWED_METHODS,
    collection_resource,
    content_type_for,
    facts_from_stat,
    file_resource,
    parse_depth,
    parse_propfind,
    render_multistatus,
    safe_stat,
    stat_file,
)

log = logging.getLogger("calibre_webdav")

_MULTISTATUS = "application/xml; charset=utf-8"

# Advertised on OPTIONS. Class 1 is plain WebDAV with no locking, which is all a
# read-only server can honestly claim.
_DAV_COMPLIANCE = "1"


async def _current_index(request: Request) -> LibraryIndex:
    """Fetch the live index off the event loop.

    Usually this just compares a timestamp and returns, but when the debounce
    window has elapsed it stats metadata.db and may run a full rebuild, so it
    does not belong on the loop.
    """
    cache: IndexCache = request.app.state.index
    return await anyio.to_thread.run_sync(cache.current)


def _options_response() -> Response:
    return Response(
        status_code=204,
        headers={
            "DAV": _DAV_COMPLIANCE,
            "Allow": ", ".join(ALLOWED_METHODS),
            "MS-Author-Via": "DAV",
        },
    )


async def _propfind_response(request: Request, resources_factory) -> Response:
    try:
        prop_request = parse_propfind(await request.body())
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=400)

    resources = await resources_factory(prop_request)
    if resources is None:
        return PlainTextResponse("Not Found", status_code=404)

    return Response(
        content=render_multistatus(resources, prop_request),
        status_code=207,
        media_type=_MULTISTATUS,
        headers={"DAV": _DAV_COMPLIANCE},
    )


def _list_members(index: LibraryIndex, path: str, mtime: float | None, *, recursive: bool) -> list:
    """Stat every member of a collection. Blocking, so it runs in a worker thread."""
    members = []
    for child in index.children(path):
        if index.is_collection(child):
            members.append(collection_resource(child, mtime))
            if recursive:
                members.extend(_list_members(index, child, mtime, recursive=True))
            continue
        entry = index.get(child)
        if entry is None:
            continue
        facts = stat_file(entry.real_path)
        if facts is None:
            # Deleted between the index build and now: omit it rather than
            # advertise a book that would 404 on GET.
            log.warning("listing: %s vanished from disk, omitting", child)
            continue
        members.append(file_resource(child, facts))
    return members


async def _collection_response(request: Request, index: LibraryIndex, path: str) -> Response:
    if request.method == "PROPFIND":
        depth = parse_depth(request.headers.get("depth"))

        async def resources(prop_request):
            # The index already carries metadata.db's mtime, so a collection's
            # own last-modified costs no extra stat.
            mtime = index.signature[0] / 1e9 if index.signature else None
            collection = collection_resource(path, mtime)
            if depth == 0:
                return [collection]
            members = await anyio.to_thread.run_sync(
                partial(_list_members, index, path, mtime, recursive=depth == "infinity")
            )
            return [collection, *members]

        return await _propfind_response(request, resources)

    # A GET on a collection has no useful body for a machine client, but
    # answering plainly beats a stack trace.
    return PlainTextResponse(
        f"{len(index.children(path))} member(s). Use a WebDAV client.\n", status_code=200
    )


async def endpoint(request: Request) -> Response:
    """One collection or one book, decided by the index rather than by the URL."""
    if request.method == "OPTIONS":
        return _options_response()

    # A trailing slash is how clients spell "collection", but the index keys
    # collections without one.
    path = request.path_params.get("path", "").strip("/")
    index = await _current_index(request)

    if index.is_collection(path):
        return await _collection_response(request, index, path)

    entry = index.get(path)

    # One stat serves both the properties and the response headers.
    stat_result = (
        None if entry is None else await anyio.to_thread.run_sync(safe_stat, entry.real_path)
    )

    if request.method == "PROPFIND":

        async def resources(prop_request):
            if stat_result is None:
                return None
            return [file_resource(path, facts_from_stat(stat_result))]

        return await _propfind_response(request, resources)

    if stat_result is None:
        if entry is not None:
            log.warning("GET %s: file vanished from disk: %s", path, entry.real_path)
        return PlainTextResponse("Not Found", status_code=404)

    assert entry is not None
    facts = facts_from_stat(stat_result)
    # etag and last-modified are passed explicitly so they match what PROPFIND
    # reported; FileResponse only fills in headers that are absent. Handing it
    # the stat result avoids a second stat and drives its Range handling.
    return FileResponse(
        entry.real_path,
        media_type=content_type_for(path),
        headers={"etag": facts.etag, "last-modified": facts.http_date},
        stat_result=stat_result,
    )


def create_app(config: Config) -> Starlette:
    cache = IndexCache(config)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Build once up front so startup problems surface immediately and the
        # first PROPFIND is not the thing that pays for the query.
        index = await anyio.to_thread.run_sync(cache.refresh)
        log.info("serving %d book(s) from %s", len(index.entries), config.library_root)
        yield

    app = Starlette(
        routes=[
            Route("/", endpoint, methods=list(ALLOWED_METHODS)),
            Route("/{path:path}", endpoint, methods=list(ALLOWED_METHODS)),
        ],
        lifespan=lifespan,
    )
    app.state.index = cache
    app.state.config = config

    if config.allow_anonymous:
        log.warning("CW_ALLOW_ANONYMOUS is set: serving without authentication")
    elif config.username is None or config.password is None:
        # Config.from_env already enforces this, but create_app also takes
        # hand-built Config objects, and failing open here would be silent.
        raise ConfigError("credentials are required unless anonymous access is enabled")
    else:
        app.add_middleware(BasicAuthMiddleware, username=config.username, password=config.password)

    return app


def _log_level(verbose: int) -> int:
    if verbose >= 4:
        return logging.DEBUG
    if verbose == 3:
        return logging.INFO
    if verbose == 2:
        return logging.WARNING
    return logging.ERROR


def main() -> int:
    try:
        config = Config.from_env()
        config.validate()
    except ConfigError as error:
        # Logging may not be configured yet, so this goes straight to stderr.
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=_log_level(config.verbose),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    import uvicorn

    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=logging.getLevelName(_log_level(config.verbose)).lower(),
        access_log=config.verbose >= 4,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
