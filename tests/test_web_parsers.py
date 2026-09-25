import pytest

from nimna.tools import ToolError
from nimna.tools.builtin.web import _assert_public_url, _clean_ddg_url, _DDGParser, _TextExtractor

DDG_HTML = """
<div class="results">
 <div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
   <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Fwhatsnew%2F3.13.html&amp;rut=abc">What&#x27;s New In Python 3.13</a>
   </h2>
   <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Fwhatsnew%2F3.13.html">Editors, Adam Turner and Thomas Wouters. <b>Python</b> 3.13 was released on October 7, 2024.</a>
  </div>
 </div>
 <div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep result__body">
   <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://www.python.org/downloads/">Download Python</a></h2>
   <div class="result__snippet">Official downloads page.</div>
  </div>
 </div>
</div>
"""


def test_ddg_parser_extracts_results_and_unwraps_redirects():
    parser = _DDGParser()
    parser.feed(DDG_HTML)
    assert len(parser.results) == 2
    first, second = parser.results
    assert first["url"] == "https://docs.python.org/3/whatsnew/3.13.html"
    assert "Python 3.13" in first["title"]
    assert "October 7, 2024" in first["snippet"]
    assert second["url"] == "https://www.python.org/downloads/"
    assert second["snippet"].strip() == "Official downloads page."


def test_clean_ddg_url_passthrough():
    assert _clean_ddg_url("https://example.org/x") == "https://example.org/x"
    assert _clean_ddg_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.b%2Fc%3Fd%3D1") == "https://a.b/c?d=1"


def test_text_extractor_strips_scripts_and_keeps_title():
    html = "<html><head><title> Hello  Page </title><style>p{}</style></head><body><nav>menu</nav><h1>Head</h1><p>Body &amp; text</p><script>alert(1)</script></body></html>"
    extractor = _TextExtractor()
    extractor.feed(html)
    text = extractor.text()
    assert " ".join(extractor.title.split()) == "Hello Page"
    assert "alert" not in text and "menu" not in text and "p{}" not in text
    assert "Head" in text and "Body & text" in text


@pytest.mark.parametrize("url", ["ftp://x.y/z", "http://localhost/", "http://127.0.0.1:8000/", "http://10.1.2.3/", "http://service.internal/"])
def test_public_url_guard(url):
    with pytest.raises(ToolError):
        _assert_public_url(url)
