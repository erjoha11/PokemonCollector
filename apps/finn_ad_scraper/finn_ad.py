"""Fetch and parse a finn.no classified ad ("annonse")."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class FinnAd:
    """Structured content extracted from a finn.no ad page."""

    url: str
    title: str
    description: str
    price: Optional[float]
    currency: Optional[str]
    images: list[str] = field(default_factory=list)
    location: Optional[str] = None
    raw_html: Optional[str] = None


class FinnAdFetchError(RuntimeError):
    """Raised when a finn.no ad page can't be fetched or understood."""


def fetch_finn_ad(url: str, *, html: Optional[str] = None, use_browser: bool = True) -> FinnAd:
    """Open a finn.no ad and extract its content.

    Parameters
    ----------
    url:
        The finn.no ad URL (e.g. https://www.finn.no/recommerce/forsale/item/123456789).
    html:
        Pre-fetched page HTML. Pass this in tests to skip network/browser access
        entirely. When omitted, the page is fetched live.
    use_browser:
        finn.no ad pages are a JS-rendered Next.js app whose photo gallery is
        populated client-side, so by default the page is opened in a real
        (headless) Chromium browser via Playwright to make sure every photo is
        present before we scrape it. Set to False to fetch with a plain HTTP
        GET instead (faster, but may miss photos beyond the JSON-LD cover
        image, and requires no browser install).
    """
    if html is None:
        html = _fetch_html(url, use_browser=use_browser)

    soup = BeautifulSoup(html, "html.parser")
    ad = _parse_json_ld(soup, url) or _parse_meta_tags(soup, url)
    if ad is None:
        raise FinnAdFetchError(
            f"Could not find recognizable ad content (JSON-LD or meta tags) at {url}"
        )
    ad.images = _dedupe_images(ad.images + _extract_gallery_images(soup))
    ad.raw_html = html
    return ad


def _fetch_html(url: str, *, use_browser: bool) -> str:
    if use_browser:
        return _fetch_html_via_browser(url)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return resp.text


def _fetch_html_via_browser(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise FinnAdFetchError(
            "playwright is required to render this page; install it with "
            "`pip install playwright && playwright install chromium`"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.content()
        finally:
            browser.close()


_FINN_IMAGE_HOST = "images.finncdn.no"
_IMAGE_SIZE_RE = re.compile(r"/(\d+)w/")


def _extract_gallery_images(soup: BeautifulSoup) -> list[str]:
    """Find every ad photo in the rendered page, not just the JSON-LD cover image.

    finn.no ad pages show a photo gallery/carousel with one <img> per photo;
    JSON-LD's `image` field often only carries a single cover shot, which
    would mean missing cards that only appear in the other photos.
    """
    images = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if _FINN_IMAGE_HOST in src:
            images.append(src)
    return images


def _dedupe_images(urls: list[str]) -> list[str]:
    """Dedupe photo URLs, keeping the highest-resolution variant of each photo.

    finn.no serves the same photo at multiple widths (e.g. .../320w/<id>.jpg
    and .../1600w/<id>.jpg); without this, the same card photo would be sent
    to card identification multiple times at different sizes.
    """
    best_size: dict[str, int] = {}
    best_url: dict[str, str] = {}
    order: list[str] = []
    for url in urls:
        if not url:
            continue
        key = url.rsplit("/", 1)[-1].split("?")[0]
        match = _IMAGE_SIZE_RE.search(url)
        size = int(match.group(1)) if match else 0
        if key not in best_size:
            order.append(key)
        if key not in best_size or size > best_size[key]:
            best_size[key] = size
            best_url[key] = url
    return [best_url[key] for key in order]


def _parse_json_ld(soup: BeautifulSoup, url: str) -> Optional[FinnAd]:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue

        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("@type", "")
            types = entry_type if isinstance(entry_type, list) else [entry_type]
            if not any(t in ("Product", "Offer", "Article") for t in types):
                continue

            title = entry.get("name") or ""
            description = entry.get("description") or ""
            images_raw = entry.get("image") or []
            images = images_raw if isinstance(images_raw, list) else [images_raw]

            offers = entry.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = _to_float(offers.get("price"))
            currency = offers.get("priceCurrency")

            if title:
                return FinnAd(
                    url=url,
                    title=title,
                    description=description,
                    price=price,
                    currency=currency,
                    images=[img for img in images if img],
                )
    return None


def _parse_meta_tags(soup: BeautifulSoup, url: str) -> Optional[FinnAd]:
    def meta(*names: str) -> Optional[str]:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find(
                "meta", attrs={"name": name}
            )
            if tag and tag.get("content"):
                return tag["content"]
        return None

    title = meta("og:title", "twitter:title")
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    if not title:
        return None

    description = meta("og:description", "twitter:description", "description") or ""
    image = meta("og:image", "twitter:image")
    images = [image] if image else []

    price = _to_float(meta("product:price:amount"))
    currency = meta("product:price:currency")
    if price is None:
        price, currency = _guess_price_from_text(soup.get_text(" ", strip=True))

    return FinnAd(
        url=url,
        title=title,
        description=description,
        price=price,
        currency=currency,
        images=images,
    )


_PRICE_RE = re.compile(r"(\d[\d\s]{1,10}\d|\d)\s?(kr|NOK)\b", re.IGNORECASE)


def _guess_price_from_text(text: str) -> tuple[Optional[float], Optional[str]]:
    match = _PRICE_RE.search(text)
    if not match:
        return None, None
    digits = match.group(1).replace(" ", "").replace("\xa0", "")
    try:
        return float(digits), "NOK"
    except ValueError:
        return None, None


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None
