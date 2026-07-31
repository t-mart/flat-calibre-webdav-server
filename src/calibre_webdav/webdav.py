"""The read-only WebDAV subset this server actually needs.

Scope is deliberately small: OPTIONS, PROPFIND, GET and HEAD. There is no PUT,
DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK or PROPPATCH.

Everything here is pure protocol: building 207 Multi-Status bodies and reading
PROPFIND request bodies. Nothing in this module knows about Calibre.
"""

import hashlib
import mimetypes
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import ParseError, fromstring

DAV_NS = "DAV:"

# Emit `D:` as the DAV prefix rather than ElementTree's default `ns0:`.
ET.register_namespace("D", DAV_NS)

# mimetypes has no entry for .mobi, and the fallback for anything unrecognised
# should be a byte stream rather than nothing at all.
_EXTRA_CONTENT_TYPES = {".mobi": "application/x-mobipocket-ebook"}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

_STATUS_OK = "HTTP/1.1 200 OK"
_STATUS_NOT_FOUND = "HTTP/1.1 404 Not Found"

# The methods this server answers. Anything else gets a 405 from the router,
# which also emits the Allow header listing these.
ALLOWED_METHODS = ("OPTIONS", "HEAD", "GET", "PROPFIND")

# 0 and 1 are the counts RFC 4918 allows; "infinity" is the rest of the subtree.
type Depth = Literal[0, 1, "infinity"]


def qname(local: str) -> str:
    return f"{{{DAV_NS}}}{local}"


@dataclass(frozen=True, slots=True)
class FileFacts:
    """The parts of a real file on disk that WebDAV reports."""

    size: int
    mtime: float
    etag: str

    @property
    def http_date(self) -> str:
        """RFC 1123 date, the format `getlastmodified` and Last-Modified use."""
        return formatdate(self.mtime, usegmt=True)

    @property
    def iso_date(self) -> str:
        """ISO 8601, which is what `creationdate` uses instead."""
        return datetime.fromtimestamp(self.mtime, UTC).isoformat().replace("+00:00", "Z")


def file_etag(size: int, mtime: float) -> str:
    """Derive an etag from the real file on disk, never from database fields.

    Deliberately identical to Starlette's FileResponse algorithm, but computed
    here so PROPFIND and GET are locked to one value: the etag is the client's
    change-detection key, so a listing that disagreed with the download would
    silently break syncing.
    """
    digest = hashlib.md5(f"{mtime}-{size}".encode(), usedforsecurity=False).hexdigest()
    return f'"{digest}"'


def facts_from_stat(stat: os.stat_result) -> FileFacts:
    return FileFacts(
        size=stat.st_size, mtime=stat.st_mtime, etag=file_etag(stat.st_size, stat.st_mtime)
    )


def stat_file(path: Path) -> FileFacts | None:
    """Stat a book, returning None if it vanished since the index was built."""
    stat = safe_stat(path)
    return None if stat is None else facts_from_stat(stat)


def safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def content_type_for(name: str) -> str:
    suffix = Path(name).suffix.casefold()
    if suffix in _EXTRA_CONTENT_TYPES:
        return _EXTRA_CONTENT_TYPES[suffix]
    return mimetypes.guess_type(name)[0] or _DEFAULT_CONTENT_TYPE


def href_for(path: str = "", *, is_collection: bool = False) -> str:
    """Percent-encode a relative path into an absolute href.

    Each component is encoded with `safe=""` because these names carry commas,
    spaces, colons-turned-underscores and non-ASCII, and every one of them has
    to survive the round trip back as a request path. The separators are added
    afterwards so they stay separators.
    """
    if not path:
        return "/"
    encoded = "/".join(quote(component, safe="") for component in path.split("/"))
    return f"/{encoded}/" if is_collection else f"/{encoded}"


@dataclass(frozen=True, slots=True)
class DavResource:
    """One `<D:response>`: an href plus the properties we can answer for it."""

    href: str
    is_collection: bool
    properties: dict[str, str]


def display_name(path: str) -> str:
    """The last component of a path, or `/` for the root collection."""
    return path.rpartition("/")[2] or "/"


def collection_resource(path: str, last_modified: float | None) -> DavResource:
    """A directory. It exists only in the index; nothing is stat'ed for it.

    Collections have no size and no etag of their own, and their last-modified
    is metadata.db's: the whole tree is derived from that one file.
    """
    properties = {"displayname": display_name(path)}
    if last_modified is not None:
        properties["getlastmodified"] = formatdate(last_modified, usegmt=True)
    return DavResource(
        href=href_for(path, is_collection=True), is_collection=True, properties=properties
    )


def file_resource(path: str, facts: FileFacts) -> DavResource:
    name = display_name(path)
    return DavResource(
        href=href_for(path),
        is_collection=False,
        properties={
            "displayname": name,
            "getcontentlength": str(facts.size),
            "getcontenttype": content_type_for(name),
            "getlastmodified": facts.http_date,
            "creationdate": facts.iso_date,
            "getetag": facts.etag,
        },
    )


@dataclass(frozen=True, slots=True)
class PropRequest:
    """A parsed PROPFIND body."""

    mode: Literal["allprop", "propname", "prop"] = "allprop"
    names: tuple[str, ...] = ()


def parse_propfind(body: bytes) -> PropRequest:
    """Parse a PROPFIND body. Raises ValueError on malformed XML.

    An absent or empty body means allprop, per RFC 4918. Parsing goes through
    defusedxml because this is attacker-shaped input even on a trusted LAN.
    """
    if not body.strip():
        return PropRequest(mode="allprop")

    try:
        root = fromstring(body.decode("utf-8", errors="replace"))
    except (ParseError, ValueError) as error:
        raise ValueError(f"malformed PROPFIND body: {error}") from error

    if root.tag != qname("propfind"):
        raise ValueError(f"expected a {{DAV:}}propfind root, got {root.tag}")

    if root.find(qname("propname")) is not None:
        return PropRequest(mode="propname")
    if root.find(qname("allprop")) is not None:
        return PropRequest(mode="allprop")

    prop = root.find(qname("prop"))
    if prop is None:
        return PropRequest(mode="allprop")
    return PropRequest(mode="prop", names=tuple(child.tag for child in prop))


def parse_depth(header: str | None) -> Depth:
    """Interpret the Depth header, defaulting to infinity as RFC 4918 says.

    Infinity is answered rather than refused: the whole tree is already in
    memory, so a recursive listing costs no more than the flat one this server
    used to serve from its root.
    """
    value = (header or "infinity").strip().casefold()
    if value == "0":
        return 0
    return 1 if value == "1" else "infinity"


def _resolve_properties(resource: DavResource, request: PropRequest) -> tuple[list[str], list[str]]:
    """Split the requested properties into ones we have and ones we don't."""
    available = ["resourcetype", *resource.properties]
    if request.mode in ("allprop", "propname"):
        return available, []

    found: list[str] = []
    missing: list[str] = []
    for tag in request.names:
        local = tag.removeprefix(f"{{{DAV_NS}}}") if tag.startswith(f"{{{DAV_NS}}}") else None
        if local is not None and local in available:
            found.append(local)
        else:
            missing.append(tag)
    return found, missing


def _append_property(
    parent: ET.Element, resource: DavResource, local: str, *, name_only: bool
) -> None:
    if local == "resourcetype":
        element = ET.SubElement(parent, qname("resourcetype"))
        # An empty resourcetype is what marks a resource as *not* a collection.
        if resource.is_collection and not name_only:
            ET.SubElement(element, qname("collection"))
        return
    element = ET.SubElement(parent, qname(local))
    if not name_only:
        element.text = resource.properties[local]


def render_multistatus(resources: list[DavResource], request: PropRequest) -> bytes:
    """Assemble a 207 Multi-Status body: one `<D:response>` per resource."""
    multistatus = ET.Element(qname("multistatus"))

    for resource in resources:
        response = ET.SubElement(multistatus, qname("response"))
        ET.SubElement(response, qname("href")).text = resource.href
        found, missing = _resolve_properties(resource, request)

        # Always emit at least one propstat, even when nothing was matched.
        if found or not missing:
            propstat = ET.SubElement(response, qname("propstat"))
            prop = ET.SubElement(propstat, qname("prop"))
            for local in found:
                _append_property(prop, resource, local, name_only=request.mode == "propname")
            ET.SubElement(propstat, qname("status")).text = _STATUS_OK

        if missing:
            propstat = ET.SubElement(response, qname("propstat"))
            prop = ET.SubElement(propstat, qname("prop"))
            for tag in missing:
                ET.SubElement(prop, tag)
            ET.SubElement(propstat, qname("status")).text = _STATUS_NOT_FOUND

    return ET.tostring(multistatus, encoding="utf-8", xml_declaration=True)
