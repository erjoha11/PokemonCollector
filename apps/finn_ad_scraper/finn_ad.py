"""Fetch a finn.no ad page and parse it into a FinnAd.

Two separate steps so each can be tested on its own:

- `fetch_html(url)` gets the page. finn.no ad pages embed a JSON-LD `Product`
  block in the server-rendered HTML, so a plain GET is enough almost always;
  only if that response lacks structured data does it render the page in
  headless Chromium (Playwright), which is optional to install.
- `parse_ad(html, url)` reads the ad out of that HTML: JSON-LD first, then
  Open Graph / meta tags as a fallback.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

import requests
from bs4 import BeautifulSoup

from .models import FinnAd

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 20
BROWSER_TIMEOUT_MS = 30_000

_AD_TYPES = {"Product", "Offer", "IndividualProduct", "Article"}


class FinnAdFetchError(RuntimeError):
    """The ad page couldn't be fetched, or held no recognizable ad content."""


def fetch_finn_ad(url: str, *, html: Optional[str] = None, use_browser: bool = True) -> FinnAd:
    """Open a finn.no ad and return its content. Pass `html` to skip the
    network entirely (tests, or a page saved earlier)."""
    if html is None:
        html = fetch_html(url, use_browser=use_browser)
    return parse_ad(html, url)


def fetch_html(url: str, *, use_browser: bool = True, session: Optional[requests.Session] = None) -> str:
    http_error: Optional[Exception] = None
    html = ""
    try:
        response = (session or requests).get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        html = response.text
        if has_structured_data(html):
            return html
    except requests.RequestException as exc:
        http_error = exc

    if use_browser:
        return _fetch_html_via_browser(url)
    if html:
        return html  # no structured data, but parse_ad may still find meta tags
    raise FinnAdFetchError(f"Could not fetch {url}: {http_error}")


def has_structured_data(html: str) -> bool:
    return "application/ld+json" in html or 'property="og:title"' in html


def _fetch_html_via_browser(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise FinnAdFetchError(
            "The page needs a browser to render, but Playwright isn't installed: "
            "run `pip install playwright && playwright install chromium`, or pass use_browser=False."
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=BROWSER_TIMEOUT_MS)
            return page.content()
        finally:
            browser.close()


def parse_ad(html: str, url: str) -> FinnAd:
    soup = BeautifulSoup(html, "html.parser")
    ad = _from_json_ld(soup, url)
    meta_ad = _from_meta_tags(soup, url)
    if ad is None and meta_ad is None:
        raise FinnAdFetchError(f"No ad content (JSON-LD or meta tags) found at {url}")
    if ad is None:
        return meta_ad
    if meta_ad is not None:
        # JSON-LD wins field by field; meta tags only fill what it left empty.
        ad.description = ad.description or meta_ad.description
        ad.images = _unique(ad.images + meta_ad.images)
        if ad.price is None:
            ad.price, ad.currency = meta_ad.price, meta_ad.currency
    return ad


def _json_ld_entries(soup: BeautifulSoup) -> Iterable[dict]:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            entry = stack.pop(0)
            if not isinstance(entry, dict):
                continue
            yield entry
            graph = entry.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)


def _from_json_ld(soup: BeautifulSoup, url: str) -> Optional[FinnAd]:
    for entry in _json_ld_entries(soup):
        types = entry.get("@type") or []
        types = set(types if isinstance(types, list) else [types])
        title = _text(entry.get("name"))
        if not (types & _AD_TYPES) or not title:
            continue
        offers = entry.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        return FinnAd(
            url=url,
            title=title,
            description=_text(entry.get("description")),
            price=_to_float(offers.get("price") if isinstance(offers, dict) else None),
            currency=(offers.get("priceCurrency") if isinstance(offers, dict) else None) or None,
            images=_unique(_image_urls(entry.get("image"))),
        )
    return None


def _from_meta_tags(soup: BeautifulSoup, url: str) -> Optional[FinnAd]:
    def meta(*names: str) -> list[str]:
        values = []
        for name in names:
            for tag in soup.find_all("meta", attrs={"property": name}) + soup.find_all("meta", attrs={"name": name}):
                if tag.get("content"):
                    values.append(tag["content"].strip())
        return values

    title = next(iter(meta("og:title", "twitter:title")), None)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        return None

    price = _to_float(next(iter(meta("product:price:amount")), None))
    currency = next(iter(meta("product:price:currency")), None)
    if price is None:
        price, currency = _price_from_text(soup.get_text(" ", strip=True))
    return FinnAd(
        url=url,
        title=title,
        description=next(iter(meta("og:description", "twitter:description", "description")), ""),
        price=price,
        currency=currency,
        images=_unique(meta("og:image", "twitter:image")),
    )


def _image_urls(value: Any) -> list[str]:
    """JSON-LD `image` may be a URL, an ImageObject, or a list of either."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        url = value.get("contentUrl") or value.get("url")
        return [url] if isinstance(url, str) else []
    if isinstance(value, list):
        return [url for item in value for url in _image_urls(item)]
    return []


_PRICE_RE = re.compile(r"(\d{1,3}(?:[  .]\d{3})+|\d+)\s?(?:kr|NOK)\b", re.IGNORECASE)


def _price_from_text(text: str) -> tuple[Optional[float], Optional[str]]:
    match = _PRICE_RE.search(text)
    if not match:
        return None, None
    return float(re.sub(r"[  .]", "", match.group(1))), "NOK"


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
