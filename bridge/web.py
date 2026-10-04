"""Reading the web without the screen: search results and page text for the agent's tools.

Only public internet addresses are fetched. A page could otherwise trick the agent into calling services
on the phone itself (e.g. the UI automation server) or on the home/Tailscale network.
"""
import html
import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

USER_AGENT = "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Mobile Safari/537.36"
SEARCH_URL = "https://html.duckduckgo.com/html/?q="
TIMEOUT_S = 20
MAX_BYTES = 2_000_000
MAX_TEXT = 6000
MAX_RESULTS = 6
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form", "iframe", "template", "select"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "section", "article", "header", "main", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt",
}
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
RESULT = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
TAG = re.compile(r"<[^>]+>")


class BlockedAddress(ValueError):
    pass


def check_public(url: str) -> None:
    """Refuse anything but http(s) to public internet addresses."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedAddress("Only http and https links can be read.")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except socket.gaierror as error:
        raise BlockedAddress(f"Couldn't find {parts.hostname}: {error}") from error
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise BlockedAddress(f"{parts.hostname} is a private or local address; only public sites can be read.")


class PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(PublicOnlyRedirects)


class TextExtractor(HTMLParser):
    """Visible text of a page, one block per line, without scripts, menus and footers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in SKIP_TAGS:
            self.skipping += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skipping:
            self.skipping -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def download(url: str) -> tuple[str, str, str]:
    """Return (final url, content type, decoded body)."""
    check_public(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with OPENER.open(request, timeout=TIMEOUT_S) as response:
        raw = response.read(MAX_BYTES)
        charset = response.headers.get_content_charset() or "utf-8"
        return response.geturl(), response.headers.get_content_type(), raw.decode(charset, errors="replace")


def fetch_text(url: str) -> str:
    """Readable text of a web page (or plain text / JSON as is)."""
    if "://" not in url:
        url = f"https://{url}"
    final_url, content_type, body = download(url)
    if "html" in content_type:
        title = TITLE.search(body)
        extractor = TextExtractor()
        extractor.feed(body)
        heading = html.unescape(" ".join(title[1].split())) if title else ""
        text = f"{heading}\n{final_url}\n\n{extractor.text()}"
    elif content_type.startswith("text/") or content_type == "application/json":
        text = f"{final_url}\n\n{body}"
    else:
        return f"{final_url} is {content_type}, which can't be read as text."
    return text[:MAX_TEXT] + ("\n[cut: page is longer]" if len(text) > MAX_TEXT else "")


def clean(fragment: str) -> str:
    return html.unescape(" ".join(TAG.sub("", fragment).split()))


def real_link(href: str) -> str:
    """DuckDuckGo wraps result links in a redirect; return the actual destination."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
    return query["uddg"][0] if "uddg" in query else href


def search(query: str) -> str:
    _, _, body = download(SEARCH_URL + urllib.parse.quote(query))
    results = []
    for href, title, snippet in RESULT.findall(body):
        link = real_link(html.unescape(href))
        if "duckduckgo.com/y.js" in link:  # ads
            continue
        results.append(f"{len(results) + 1}. {clean(title)}\n   {link}\n   {clean(snippet)}")
        if len(results) == MAX_RESULTS:
            break
    return "\n".join(results) or f"No results for {query!r}."
