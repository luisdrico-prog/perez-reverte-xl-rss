#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

BASE = "https://www.abc.es"
AUTHOR_URL = f"{BASE}/xlsemanal/firmas/arturo-perez-reverte/"
PREFIX = "/xlsemanal/firmas/arturo-perez-reverte/"
CATALOG_FILE = Path("catalog.json")
FEED_FILE = Path("feed.xml")
STATE_FILE = Path("state.json")
BACKFILL_CLICKS = int(os.getenv("BACKFILL_CLICKS", "120"))
INCREMENTAL_CLICKS = int(os.getenv("INCREMENTAL_CLICKS", "1"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.20"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/151.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
}

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
    "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
    "octubre": 10, "noviembre": 11, "diciembre": 12,
}

def log(msg: str) -> None:
    print(msg, flush=True)

def canonical(url: str) -> str | None:
    if not url:
        return None
    u = urljoin(BASE, url).split("#", 1)[0].split("?", 1)[0]
    if PREFIX not in u or not u.endswith(".html"):
        return None
    return u

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

async def dismiss_banners(page) -> None:
    for pat in [r"aceptar", r"aceptar y continuar", r"consentir"]:
        try:
            loc = page.get_by_role("button", name=re.compile(pat, re.I))
            for i in range(await loc.count()):
                el = loc.nth(i)
                if await el.is_visible():
                    await el.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    return
        except Exception:
            pass

async def collect_author_links() -> list[str]:
    first_run = not CATALOG_FILE.exists()
    max_clicks = BACKFILL_CLICKS if first_run else INCREMENTAL_CLICKS
    log("Modo hemeroteca inicial." if first_run else "Modo incremental.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="es-ES",
            viewport={"width": 1440, "height": 1200},
            user_agent=HEADERS["User-Agent"],
        )
        page = await context.new_page()
        await page.goto(AUTHOR_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1200)
        await dismiss_banners(page)

        last_count = -1
        stagnant = 0

        for n in range(max_clicks + 1):
            hrefs = await page.locator("a[href]").evaluate_all("(els) => els.map(a => a.href)")
            links = {u for h in hrefs if (u := canonical(h))}
            log(f"Enlaces detectados: {len(links)}")

            if n >= max_clicks:
                break

            if len(links) == last_count:
                stagnant += 1
            else:
                stagnant = 0
            last_count = len(links)

            if stagnant >= 3:
                break

            more = page.get_by_text(re.compile(r"VER\s+M[AÁ]S\s+NOTICIAS", re.I))
            visible = None
            try:
                for i in range(await more.count()):
                    c = more.nth(i)
                    if await c.is_visible():
                        visible = c
                        break
            except Exception:
                pass

            if visible is None:
                break

            try:
                await visible.click(force=True, timeout=5000)
                await page.wait_for_timeout(1100)
            except Exception:
                break

        hrefs = await page.locator("a[href]").evaluate_all("(els) => els.map(a => a.href)")
        links = sorted({u for h in hrefs if (u := canonical(h))})
        await browser.close()

    return links

def parse_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def parse_spanish_visible_date(text: str) -> str | None:
    m = re.search(
        r"\b(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+)\s+"
        r"(\d{4}),\s*(\d{1,2}):(\d{2})h",
        text, re.I
    )
    if not m:
        return None
    month_name = (m.group(2).lower()
                  .replace("á","a").replace("é","e").replace("í","i")
                  .replace("ó","o").replace("ú","u").replace("ü","u"))
    month = MONTHS.get(month_name)
    if not month:
        return None
    return datetime(
        int(m.group(3)), month, int(m.group(1)),
        int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc
    ).isoformat()

def jsonld_objects(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, list):
            for x in data:
                if isinstance(x, dict):
                    yield x
        elif isinstance(data, dict):
            if isinstance(data.get("@graph"), list):
                for x in data["@graph"]:
                    if isinstance(x, dict):
                        yield x
            yield data

def extract_article(url: str) -> dict:
    r = requests.get(url, headers=HEADERS, timeout=35)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*keys):
        for key in keys:
            for attr in ("property", "name"):
                tag = soup.find("meta", attrs={attr: key})
                if tag and tag.get("content"):
                    return tag["content"].strip()
        return ""

    title = meta("og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else url
    title = re.sub(r"\s*-\s*XLSemanal\s*-\s*Abc\s*$", "", title, flags=re.I).strip()

    description = meta("og:description", "twitter:description", "description")
    image = meta("og:image", "twitter:image")

    published = parse_iso_date(meta("article:published_time", "datePublished"))
    if not published:
        for obj in jsonld_objects(soup):
            published = parse_iso_date(obj.get("datePublished"))
            if published:
                break
    if not published:
        published = parse_spanish_visible_date(soup.get_text(" ", strip=True))
    if not published:
        published = datetime.now(timezone.utc).isoformat()

    return {
        "url": url,
        "title": html.unescape(title),
        "description": html.unescape(description or ""),
        "image": image,
        "published": published,
        "author": "Arturo Pérez-Reverte",
        "section": "Patente de corso",
        "discovered_at": datetime.now(timezone.utc).isoformat(),
    }

def update_catalog(links: list[str]) -> list[dict]:
    old = load_json(CATALOG_FILE, [])
    by_url = {x.get("url"): x for x in old if isinstance(x, dict) and x.get("url")}
    new_count = 0

    for idx, url in enumerate(links, 1):
        if url in by_url:
            continue
        log(f"[{idx}/{len(links)}] {url}")
        try:
            by_url[url] = extract_article(url)
            new_count += 1
        except Exception as exc:
            log(f"Aviso: {exc}")
        time.sleep(REQUEST_DELAY)

    catalog = list(by_url.values())
    catalog.sort(key=lambda x: x.get("published") or "", reverse=True)
    log(f"Catálogo total: {len(catalog)} | nuevos: {new_count}")
    return catalog

def parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime(1970,1,1,tzinfo=timezone.utc)

def xml_escape(value) -> str:
    return html.escape(str(value or ""), quote=True)

def cdata(value: str) -> str:
    return "<![CDATA[" + (value or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"

def build_feed(catalog: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title><![CDATA[Arturo Pérez-Reverte — XLSemanal]]></title>",
        f"<link>{xml_escape(AUTHOR_URL)}</link>",
        "<description><![CDATA[Patente de corso: artículos de Arturo Pérez-Reverte en XLSemanal/ABC.]]></description>",
        "<language>es-es</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        '<atom:link href="https://raw.githubusercontent.com/luisdrico-prog/perez-reverte-xl-rss/main/feed.xml" rel="self" type="application/rss+xml" />',
    ]

    for item in catalog:
        dt = parse_dt(item.get("published") or "")
        desc = item.get("description") or "Abrir artículo en XLSemanal."
        body = []
        if item.get("image"):
            body.append(
                f'<p><img src="{xml_escape(item["image"])}" alt="{xml_escape(item["title"])}" /></p>'
            )
        body.append(f"<p>{html.escape(desc)}</p>")
        body.append(f'<p><a href="{xml_escape(item["url"])}">Leer en XLSemanal</a></p>')

        parts.extend([
            "<item>",
            f"<title>{cdata(item['title'])}</title>",
            f"<link>{xml_escape(item['url'])}</link>",
            f'<guid isPermaLink="true">{xml_escape(item["url"])}</guid>',
            f"<pubDate>{format_datetime(dt)}</pubDate>",
            "<author>Arturo Pérez-Reverte</author>",
            "<category><![CDATA[Patente de corso]]></category>",
            f"<description>{cdata(''.join(body))}</description>",
            "</item>",
        ])

    parts.extend(["</channel>", "</rss>"])
    FEED_FILE.write_text("\n".join(parts) + "\n", encoding="utf-8")

async def main_async():
    links = await collect_author_links()
    if not links:
        raise RuntimeError("No se localizaron artículos.")
    catalog = update_catalog(links)
    save_json(CATALOG_FILE, catalog)
    build_feed(catalog)
    save_json(STATE_FILE, {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "catalog_total": len(catalog),
        "links_seen_this_run": len(links),
    })

if __name__ == "__main__":
    asyncio.run(main_async())
