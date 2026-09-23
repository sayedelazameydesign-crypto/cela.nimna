"""Web tools: keyless search (DuckDuckGo HTML endpoint) and page fetching."""
import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, urlparse, quote_plus

import httpx
from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 nimna-agent/0.1"
)


class SearchParams(BaseModel):
    query: str = Field(..., min_length=2, max_length=500, description="Search query.")
    max_results: int = Field(5, ge=1, le=10)
    region: str = Field("wt-wt", description="DuckDuckGo region code, e.g. 'xa-ar' for Arabic, 'us-en'.")


class FetchParams(BaseModel):
    url: str = Field(..., max_length=2000, description="Absolute http(s) URL to fetch.")
    max_chars: int = Field(8000, ge=200, le=60000, description="Maximum characters of extracted text.")


class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: Optional[dict[str, str]] = None
        self._capture: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._current = {"title": "", "url": _clean_ddg_url(attrs.get("href") or ""), "snippet": ""}
            self._capture = "title"
        elif tag == "a" and "result__snippet" in classes and self._current is not None:
            self._capture = "snippet"
        elif tag == "div" and "result__snippet" in classes and self._current is not None:
            self._capture = "snippet"

    def handle_endtag(self, tag):
        if tag == "a" and self._capture == "title":
            self._capture = None
        elif tag in {"a", "div"} and self._capture == "snippet":
            self._capture = None
            if self._current is not None:
                self.results.append(self._current)
                self._current = None

    def handle_data(self, data):
        if self._current is not None and self._capture:
            self._current[self._capture] += data


def _clean_ddg_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts))
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("only absolute http(s) URLs are allowed")
    host = parsed.hostname.lower()
    # block obvious local names even before DNS
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "::ffff:127.0.0.1"}:
        raise ToolError("fetching local/internal hosts is not allowed")
    if host.endswith(".local") or host.endswith(".internal") or host.endswith(".localhost"):
        raise ToolError("fetching local/internal hosts is not allowed")
    # literal IP: check without DNS round-trip
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ToolError("fetching private network addresses is not allowed")
        return
    except ValueError:
        pass
    # hostname: resolve and check every address
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f"cannot resolve host '{host}': {exc}")
    private = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            private.append(str(ip))
    if private:
        raise ToolError(f"host '{host}' resolves to private address {private[0]} – fetching blocked")


def register(registry: ToolRegistry) -> None:
    @registry.tool("web_search", "Search the web (DuckDuckGo, no API key) and return titles, URLs and snippets.",
                   SearchParams, tags=["web"])
    def web_search(params: SearchParams, ctx: ToolContext):
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(params.query)}&kl={quote_plus(params.region)}"
        try:
            response = httpx.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ar,en;q=0.8"},
                                 timeout=20, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ToolError(f"search request failed: {exc}")
        if response.status_code != 200:
            raise ToolError(f"search engine returned HTTP {response.status_code}")
        parser = _DDGParser()
        parser.feed(response.text)
        results = []
        for item in parser.results:
            title = " ".join(item["title"].split())
            snippet = " ".join(item["snippet"].split())
            if title and item["url"].startswith("http"):
                # safety: do not return private-URL results
                try:
                    _assert_public_url(item["url"])
                except ToolError:
                    continue
                results.append({"title": title, "url": item["url"], "snippet": snippet})
            if len(results) >= params.max_results:
                break
        if not results and "anomaly" in response.text.lower():
            raise ToolError("search engine asked for a CAPTCHA; retry later or use fetch_url with a known site")
        return {"query": params.query, "results": results}

    @registry.tool("fetch_url", "Download a public web page and return its readable text (scripts/styles removed).",
                   FetchParams, tags=["web"])
    def fetch_url(params: FetchParams, ctx: ToolContext):
        _assert_public_url(params.url)
        # manual redirect loop so we can validate each hop
        current = params.url
        for _ in range(5):
            try:
                with httpx.stream("GET", current, headers={"User-Agent": USER_AGENT}, timeout=20,
                                  follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ToolError(f"redirect without location for {current}")
                        # resolve relative redirects
                        from urllib.parse import urljoin
                        nxt = urljoin(current, location)
                        _assert_public_url(nxt)
                        current = nxt
                        continue
                    if response.status_code >= 400:
                        raise ToolError(f"HTTP {response.status_code} for {current}")
                    content_type = response.headers.get("content-type", "")
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > 2_000_000:
                            break
                    body = b"".join(chunks)
                    final_url = str(response.url) if str(response.url) != current else current
                    break
            except httpx.HTTPError as exc:
                raise ToolError(f"fetch failed: {exc}")
        else:
            raise ToolError("too many redirects")
        # double-check final URL is still public
        _assert_public_url(final_url)
        text = body.decode("utf-8", errors="replace")
        title = ""
        if "html" in content_type or text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
            extractor = _TextExtractor()
            extractor.feed(text)
            title = " ".join(extractor.title.split())
            text = extractor.text()
        clipped = text[: params.max_chars]
        return {
            "url": final_url,
            "title": title,
            "content_type": content_type.split(";")[0],
            "total_chars": len(text),
            "truncated": len(text) > params.max_chars,
            "content": clipped,
        }
