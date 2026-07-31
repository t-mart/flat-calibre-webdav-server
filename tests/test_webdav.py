"""End-to-end protocol tests against the real ASGI app."""

from xml.etree import ElementTree as ET

import pytest
from starlette.testclient import TestClient

from calibre_webdav.app import create_app
from calibre_webdav.config import ConfigError

DAV = "DAV:"
AUTH = ("user", "pass")


def qname(local: str) -> str:
    return f"{{{DAV}}}{local}"


@pytest.fixture
def client(library):
    library.add_book("Dune", "Herbert, Frank", series="Dune", series_index=1.0)
    library.add_book("Dune: House Atreides", "Herbert, Brian")
    library.add_book("2666", "Bolaño, Roberto", content=b"a" * 5000)
    with TestClient(create_app(library.config()), backend_options={}) as test_client:
        yield test_client


def propfind(client, path="/", depth="1", body=None, **kwargs):
    headers = {"Depth": depth} | kwargs.pop("headers", {})
    return client.request("PROPFIND", path, headers=headers, content=body, auth=AUTH, **kwargs)


def child(element: ET.Element, local: str) -> ET.Element:
    """Find a required DAV child, failing loudly rather than returning None."""
    found = element.find(qname(local))
    assert found is not None, f"expected a <{local}> child"
    return found


def text_of(element: ET.Element) -> str:
    return element.text or ""


def responses(xml: bytes) -> dict[str, ET.Element]:
    """Map href -> <response> element."""
    root = ET.fromstring(xml)
    assert root.tag == qname("multistatus")
    return {text_of(child(r, "href")): r for r in root.findall(qname("response"))}


def resourcetype_of(response: ET.Element) -> ET.Element:
    found = response.find(f".//{qname('resourcetype')}")
    assert found is not None, "every response must carry a resourcetype"
    return found


def prop_text(response: ET.Element, local: str) -> str | None:
    for propstat in response.findall(qname("propstat")):
        status = text_of(child(propstat, "status"))
        found = child(propstat, "prop").find(qname(local))
        if found is not None and "200" in status:
            return found.text
    return None


def books(client) -> dict[str, ET.Element]:
    """Every non-collection response, from one recursive PROPFIND of the root.

    Collections are exactly the hrefs with a trailing slash, which is the same
    signal a real client goes by.
    """
    found = responses(propfind(client, depth="infinity").content)
    return {href: response for href, response in found.items() if not href.endswith("/")}


def a_book(client) -> str:
    return next(iter(books(client)))


class TestAuth:
    def test_unauthenticated_request_is_challenged(self, client):
        response = client.request("PROPFIND", "/", headers={"Depth": "1"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")

    def test_wrong_password_is_rejected(self, client):
        response = client.request("PROPFIND", "/", auth=("user", "nope"))
        assert response.status_code == 401

    def test_correct_credentials_are_accepted(self, client):
        assert propfind(client).status_code == 207

    def test_anonymous_mode_needs_no_credentials(self, library):
        library.add_book("Dune", "Herbert, Frank")
        config = library.config(allow_anonymous=True, username=None, password=None)
        with TestClient(create_app(config)) as anon:
            assert anon.request("PROPFIND", "/", headers={"Depth": "1"}).status_code == 207

    def test_missing_credentials_refuse_to_build_an_open_server(self, library):
        # Failing open here would be silent, so it is a hard error instead.
        config = library.config(allow_anonymous=False, username=None, password=None)
        with pytest.raises(ConfigError):
            create_app(config)


class TestOptions:
    def test_advertises_dav_class_1(self, client):
        response = client.request("OPTIONS", "/", auth=AUTH)
        assert response.status_code == 204
        assert response.headers["dav"] == "1"

    def test_allows_exactly_the_read_only_methods(self, client):
        allow = client.request("OPTIONS", "/", auth=AUTH).headers["allow"]
        assert set(allow.replace(" ", "").split(",")) == {"OPTIONS", "HEAD", "GET", "PROPFIND"}


class TestPropfindRoot:
    def test_depth_1_lists_the_top_level_only(self, client):
        response = propfind(client, depth="1")
        assert response.status_code == 207
        assert response.headers["content-type"].startswith("application/xml")
        found = responses(response.content)
        # The root itself plus one collection per author, and no books: the
        # default template puts every book at least one level down.
        assert set(found) == {
            "/",
            "/Herbert%2C%20Frank/",
            "/Herbert%2C%20Brian/",
            "/Bola%C3%B1o%2C%20Roberto/",
        }

    def test_depth_0_returns_only_the_collection(self, client):
        found = responses(propfind(client, depth="0").content)
        assert list(found) == ["/"]

    def test_collection_is_marked_as_a_collection(self, client):
        found = responses(propfind(client, depth="0").content)
        resourcetype = resourcetype_of(found["/"])
        assert resourcetype.find(qname("collection")) is not None

    def test_depth_infinity_walks_the_whole_tree(self, client):
        found = responses(propfind(client, depth="infinity").content)
        assert set(found) == {
            "/",
            "/Herbert%2C%20Frank/",
            "/Herbert%2C%20Frank/Dune/",
            "/Herbert%2C%20Frank/Dune/01%20-%20Dune.epub",
            "/Herbert%2C%20Brian/",
            "/Herbert%2C%20Brian/Dune_%20House%20Atreides.epub",
            "/Bola%C3%B1o%2C%20Roberto/",
            "/Bola%C3%B1o%2C%20Roberto/2666.epub",
        }

    def test_an_absent_depth_header_means_infinity(self, client):
        # RFC 4918's default, and affordable because the tree is in memory.
        response = client.request("PROPFIND", "/", auth=AUTH)
        assert len(responses(response.content)) == 8

    def test_books_are_not_collections(self, client):
        for href, response in books(client).items():
            # An empty resourcetype is exactly what marks a non-collection.
            assert list(resourcetype_of(response)) == [], f"{href} must not be a collection"

    def test_every_book_carries_the_required_properties(self, client):
        for href, response in books(client).items():
            for prop in ("getcontentlength", "getlastmodified", "getetag", "getcontenttype"):
                assert prop_text(response, prop), f"{href} missing {prop}"

    def test_content_type_is_epub(self, client):
        assert prop_text(books(client)[a_book(client)], "getcontenttype") == "application/epub+zip"

    def test_a_flat_template_serves_books_from_the_root(self, library):
        library.add_book("Dune", "Herbert, Frank")
        flat = "{author_sort} - {title}.{ext}"
        with TestClient(create_app(library.config(template=flat))) as flat_client:
            found = responses(propfind(flat_client, depth="1").content)
        assert set(found) == {"/", "/Herbert%2C%20Frank%20-%20Dune.epub"}


class TestPropfindCollection:
    def test_depth_0_on_a_subcollection(self, client):
        found = responses(propfind(client, "/Herbert%2C%20Frank/", depth="0").content)
        assert list(found) == ["/Herbert%2C%20Frank/"]

    def test_depth_1_lists_immediate_members_only(self, client):
        found = responses(propfind(client, "/Herbert%2C%20Frank/", depth="1").content)
        assert set(found) == {"/Herbert%2C%20Frank/", "/Herbert%2C%20Frank/Dune/"}

    def test_a_collection_resolves_without_its_trailing_slash(self, client):
        found = responses(propfind(client, "/Herbert%2C%20Frank", depth="0").content)
        # The href in the answer always carries the slash, whatever was asked for.
        assert list(found) == ["/Herbert%2C%20Frank/"]

    def test_displayname_is_the_last_component(self, client):
        found = responses(propfind(client, "/Herbert%2C%20Frank/Dune/", depth="0").content)
        assert prop_text(found["/Herbert%2C%20Frank/Dune/"], "displayname") == "Dune"

    def test_missing_collection_is_404(self, client):
        assert propfind(client, "/Nobody%2C%20A/", depth="0").status_code == 404

    def test_a_collection_has_no_size_or_etag(self, client):
        found = responses(propfind(client, "/Herbert%2C%20Frank/", depth="0").content)
        collection = found["/Herbert%2C%20Frank/"]
        assert prop_text(collection, "getcontentlength") is None
        assert prop_text(collection, "getetag") is None


class TestPathSafety:
    @pytest.mark.parametrize(
        "path",
        [
            "/../metadata.db",
            "/..%2F..%2Fetc%2Fpasswd",
            "/Herbert%2C%20Frank/../../metadata.db",
            "/metadata.db",
        ],
    )
    def test_nothing_outside_the_index_is_reachable(self, client, path):
        # Paths resolve against the index, never against the filesystem, so a
        # traversal attempt is just a name that was never advertised.
        assert client.get(path, auth=AUTH).status_code == 404


class TestHrefEncoding:
    def test_colon_and_comma_and_space_are_encoded(self, client):
        assert "/Herbert%2C%20Brian/Dune_%20House%20Atreides.epub" in books(client)

    def test_non_ascii_is_percent_encoded_as_utf8(self, client):
        assert "/Bola%C3%B1o%2C%20Roberto/2666.epub" in books(client)

    def test_separators_are_not_encoded(self, client):
        # `%2F` would make the whole path one component to the client.
        assert all("%2F" not in href.upper() for href in books(client))

    def test_every_advertised_href_is_fetchable(self, client):
        # The real round-trip guarantee: whatever we advertise must resolve.
        hrefs = [h for h in responses(propfind(client, depth="infinity").content) if h != "/"]
        assert hrefs
        for href in hrefs:
            assert client.head(href, auth=AUTH).status_code == 200, href


class TestPropfindMember:
    def test_depth_0_on_a_book(self, client):
        href = a_book(client)
        found = responses(propfind(client, href, depth="0").content)
        assert list(found) == [href]

    def test_missing_book_is_404(self, client):
        assert propfind(client, "/nope.epub", depth="0").status_code == 404

    def test_named_properties_are_returned(self, client):
        href = a_book(client)
        body = (
            b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop>'
            b"<D:getcontentlength/><D:getetag/></D:prop></D:propfind>"
        )
        found = responses(propfind(client, href, depth="0", body=body).content)
        assert prop_text(found[href], "getcontentlength")
        assert prop_text(found[href], "getetag")

    def test_unknown_property_gets_a_404_propstat(self, client):
        href = a_book(client)
        body = (
            b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop>'
            b"<D:quota-available-bytes/></D:prop></D:propfind>"
        )
        found = responses(propfind(client, href, depth="0", body=body).content)
        statuses = [text_of(child(p, "status")) for p in found[href].findall(qname("propstat"))]
        assert any("404" in status for status in statuses)

    def test_propname_returns_names_without_values(self, client):
        href = a_book(client)
        body = b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:propname/></D:propfind>'
        found = responses(propfind(client, href, depth="0", body=body).content)
        length = found[href].find(f".//{qname('getcontentlength')}")
        assert length is not None
        assert not length.text

    def test_malformed_body_is_400(self, client):
        assert propfind(client, "/", body=b"<not xml").status_code == 400


class TestGet:
    def test_book_downloads(self, client):
        href = a_book(client)
        response = client.get(href, auth=AUTH)
        assert response.status_code == 200
        assert response.content

    def test_head_carries_length_without_a_body(self, client):
        href = a_book(client)
        response = client.head(href, auth=AUTH)
        assert response.status_code == 200
        assert int(response.headers["content-length"]) > 0
        assert response.content == b""

    def test_missing_book_is_404(self, client):
        assert client.get("/nope.epub", auth=AUTH).status_code == 404

    def test_etag_matches_the_one_propfind_reported(self, client):
        # A listing that disagreed with the download would silently break any
        # client using the etag for change detection.
        href, response = next(iter(books(client).items()))
        assert client.head(href, auth=AUTH).headers["etag"] == prop_text(response, "getetag")

    def test_content_length_matches_propfind(self, client):
        href, response = next(iter(books(client).items()))
        head = client.head(href, auth=AUTH)
        assert head.headers["content-length"] == prop_text(response, "getcontentlength")


class TestRanges:
    def _big_book(self, client):
        return next(h for h in books(client) if "2666" in h)

    def test_accept_ranges_is_advertised(self, client):
        response = client.head(self._big_book(client), auth=AUTH)
        assert response.headers["accept-ranges"] == "bytes"

    def test_range_returns_206_with_content_range(self, client):
        href = self._big_book(client)
        response = client.get(href, headers={"Range": "bytes=0-1023"}, auth=AUTH)
        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 0-1023/5000"
        assert len(response.content) == 1024

    def test_suffix_range(self, client):
        response = client.get(self._big_book(client), headers={"Range": "bytes=4900-"}, auth=AUTH)
        assert response.status_code == 206
        assert len(response.content) == 100

    def test_unsatisfiable_range_is_416(self, client):
        response = client.get(self._big_book(client), headers={"Range": "bytes=99999-"}, auth=AUTH)
        assert response.status_code == 416


class TestReadOnly:
    @pytest.mark.parametrize(
        "method", ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"]
    )
    def test_write_methods_are_rejected(self, client, method):
        href = a_book(client)
        for target in ("/", href):
            response = client.request(method, target, auth=AUTH, content=b"")
            assert response.status_code in (403, 405), f"{method} {target}"

    def test_rejection_advertises_the_allowed_methods(self, client):
        response = client.request("PUT", "/", auth=AUTH, content=b"")
        assert response.status_code == 405
        assert "PROPFIND" in response.headers["allow"]


class TestFreshness:
    def test_a_new_book_appears_without_a_restart(self, client, library):
        assert len(books(client)) == 3
        library.add_book("Hatchet", "Paulsen, Gary")
        assert len(books(client)) == 4
