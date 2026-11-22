from bs4 import BeautifulSoup

from rtshf.connectors.website import parse_live_page, parse_sheet_call

SHEET = """<html><body><article><header><h1 class="post__title">Wynn</h1></header>
<section class="sheet sheet--neutral"><div class="sheet__head"><div class="sheet__id"><span class="sheet__ticker">WYNN</span>
<span class="sheet__company">Wynn Resorts · NASDAQ</span></div><span class="pill pill--neutral pill--lg">Watchlist</span></div>
<dl class="sheet__facts"><div><dt>Price, 12 Oct 2026</dt><dd>$74.96</dd></div>
<div><dt>Base-case value (SOTP)</dt><dd>$75 <span>+0%</span></dd></div>
<div><dt>Probability-weighted (SOTP)</dt><dd>$70 <span>−7%</span></dd></div>
<div><dt>DCF base / weighted</dt><dd>$87 / $79</dd></div>
<div><dt>Horizon</dt><dd>3 years</dd></div></dl>
<figure class="range"><div class="range__lbl"><b>Bear</b><span>$38 · 25%</span></div><div class="range__lbl"><b>Base</b><span>$75 · 45%</span></div></figure>
</section><div class="post__body"><p>Text.</p></div></article></body></html>"""


def test_sheet_call_ignores_secondary_methods():
    call = parse_sheet_call(BeautifulSoup(SHEET, "html.parser"))
    assert call["rating"] == "watchlist" and call["price"] == 74.96
    assert call["target"] == 75 and call["weighted"] == 70
    assert call["scenarios"] == [{"label": "Bear", "value": 38.0, "prob": 25}, {"label": "Base", "value": 75.0, "prob": 45}]


def test_live_page_tickers():
    page = parse_live_page("https://example.org/research/wynn/", SHEET)
    assert page.ticker == "WYNN" and page.tickers == ["WYNN"] and page.call["weighted"] == 70
