#!/usr/bin/env python3
"""
Scraper des agences Laforêt Immobilier (France entière).

Étapes :
  1. Découverte des fiches agence (/agence-immobiliere/<slug>) via :
       - le sitemap (robots.txt -> sitemaps) si disponible
       - un crawl des pages "hub" : /agence-immobiliere, /regions/*, /agences-immobilieres/*
  2. Pour chaque fiche : nom, adresse, téléphone, métiers/services, email (si présent).

Sortie : agences_laforet.csv (UTF-8 avec BOM, ouvrable directement dans Excel).
"""

import csv
import os
import re
import sys
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://www.laforet.com"
OUTPUT = os.getenv("OUTPUT_CSV", "agences_laforet.csv")
WORKERS = int(os.getenv("WORKERS", "4"))
DELAY = float(os.getenv("DELAY", "0.3"))          # pause par requête et par worker
LIMIT = int(os.getenv("LIMIT", "0"))              # 0 = pas de limite (utile pour tester)
SCRAPE_LEGAL = os.getenv("SCRAPE_LEGAL", "0") == "1"  # chercher l'email aussi dans /mentions-legales

REGIONS = [
    "auvergne-rhone-alpes", "bourgogne-franche-comte", "bretagne", "centre-val-de-loire",
    "corse", "grand-est", "guadeloupe", "guyane", "hauts-de-france", "ile-de-france",
    "martinique", "normandie", "nouvelle-aquitaine", "occitanie", "pays-de-la-loire",
    "provence-alpes-cote-dazur",
]

# /agence-immobiliere/<slug>  (et rien derrière : pas d'annonce, blog, etc.)
AGENCY_RE = re.compile(r"^/agence-immobiliere/([^/?#]+)/?$")
# pages "hub" à explorer pour trouver des agences
HUB_RE = re.compile(r"^/(regions|departements?|agences-immobilieres)(/|$)")

# emails génériques du réseau / assureur : ce ne sont pas ceux de l'agence
EMAIL_BLACKLIST = {
    "donneespersonnelles@laforet.com",
    "reclamations@stpierreassurances.com",
}
EMAIL_BLACKLIST_DOMAINS = {"stpierreassurances.com", "mhtml.blink", "sentry.io", "example.com"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
FILE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "s"):
        s = requests.Session()
        retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Accept-Language": "fr-FR,fr;q=0.9",
        })
        _local.s = s
    return _local.s


def get(url: str):
    time.sleep(DELAY)
    try:
        r = session().get(url, timeout=30)
    except requests.RequestException as e:
        print(f"  ! erreur réseau {url}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code} {url}", file=sys.stderr)
        return None
    r.encoding = "utf-8"
    return r


def clean_url(href: str, base: str) -> str:
    """URL absolue, sans fragment ni paramètres de tracking (?_gl=...)."""
    u = urlparse(urljoin(base, href))
    return urlunparse((u.scheme, u.netloc, u.path.rstrip("/") or "/", "", u.query if "page=" in u.query else "", ""))


# --------------------------------------------------------------------------- #
# 1. Découverte des agences
# --------------------------------------------------------------------------- #
def agencies_from_sitemaps() -> set[str]:
    found: set[str] = set()
    r = get(f"{BASE}/robots.txt")
    sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", r.text) if r else []
    sitemaps = sitemaps or [f"{BASE}/sitemap.xml"]
    queue, seen = deque(sitemaps), set()
    while queue and len(seen) < 60:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        resp = get(sm)
        if not resp:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text):
            path = urlparse(loc).path
            if loc.endswith(".xml") or loc.endswith(".xml.gz"):
                queue.append(loc)
            elif AGENCY_RE.match(path):
                found.add(f"{BASE}{path.rstrip('/')}")
    print(f"[sitemap] {len(found)} agences")
    return found


def agencies_from_crawl() -> set[str]:
    seeds = [f"{BASE}/agence-immobiliere"] + [f"{BASE}/regions/{r}" for r in REGIONS]
    queue, visited, agencies = deque(seeds), set(), set()
    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            link = clean_url(a["href"], url)
            p = urlparse(link)
            if p.netloc != urlparse(BASE).netloc:
                continue
            if AGENCY_RE.match(p.path):
                agencies.add(f"{BASE}{p.path}")
            elif HUB_RE.match(p.path) and link not in visited:
                queue.append(link)
        print(f"[crawl] {len(visited)} pages visitées, {len(agencies)} agences ({url})")
    return agencies


# --------------------------------------------------------------------------- #
# 2. Parsing d'une fiche agence
# --------------------------------------------------------------------------- #
def fmt_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("33") and len(digits) == 11:  # +33 2 31 93 51 72 -> 02 31 93 51 72
        digits = "0" + digits[2:]
    if len(digits) == 10:
        return " ".join(digits[i:i + 2] for i in range(0, 10, 2))
    return raw.strip()


def valid_email(e: str) -> bool:
    e = e.strip().strip(".").lower()
    return (e not in EMAIL_BLACKLIST
            and e.split("@")[-1] not in EMAIL_BLACKLIST_DOMAINS
            and not e.endswith(FILE_EXT))


def find_email(soup: BeautifulSoup) -> str:
    for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
        e = a["href"][7:].split("?")[0].strip()
        if EMAIL_RE.fullmatch(e) and valid_email(e):
            return e.lower()
    for e in EMAIL_RE.findall(soup.get_text(" ")):
        if valid_email(e):
            return e.strip(".").lower()
    return ""


def parse_agency(url: str) -> dict | None:
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    h1 = soup.find("h1")
    if not h1:
        return None
    name = re.sub(r"\s+", " ", h1.get_text(" ", strip=True))

    # Adresse : "109 avenue de Saxe, 69003 LYON 3EME" (juste sous le h1)
    address = ""
    container = h1.parent
    for node in container.find_all(["span", "p", "div", "address"]):
        txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if re.search(r",\s*\d{5}\s+\S", txt) and len(txt) < 150 and not node.find(["span", "p", "div"]):
            address = txt
            break

    # Téléphone de l'agence : lien tel: "Afficher le téléphone" / "Téléphone"
    # (les liens "Appeler" des cartes d'annonces sont ceux d'AUTRES agences : on les ignore)
    phone = ""
    for a in soup.find_all("a", href=re.compile(r"^tel:")):
        label = a.get_text(" ", strip=True).lower()
        if "phone" in label or "afficher" in label:
            phone = fmt_phone(a["href"][4:])
            break

    # Métiers : liens sous le titre "Métiers :"
    services = []
    h2 = next((h for h in soup.find_all("h2") if "tiers" in h.get_text()), None)
    if h2 and h2.parent:
        services = [re.sub(r"\s+", " ", a.get_text(" ", strip=True)) for a in h2.parent.find_all("a")]
        services = [s for s in services if s]

    email = find_email(soup)
    if not email and SCRAPE_LEGAL:
        legal = get(url + "/mentions-legales")
        if legal:
            email = find_email(BeautifulSoup(legal.text, "lxml"))

    return {
        "nom": name,
        "adresse": address,
        "telephone": phone,
        "metiers": " | ".join(services),
        "email": email,
        "url": url,
    }


# --------------------------------------------------------------------------- #
def main() -> None:
    urls = agencies_from_sitemaps() | agencies_from_crawl()
    urls = sorted(urls)
    if LIMIT:
        urls = urls[:LIMIT]
    print(f"\n=== {len(urls)} fiches agence à scraper ===\n")
    if len(urls) < 100:
        print("ATTENTION : très peu d'agences trouvées, la découverte est probablement incomplète.",
              file=sys.stderr)

    rows, failed = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(parse_agency, u): u for u in urls}
        for i, f in enumerate(as_completed(futs), 1):
            u = futs[f]
            try:
                row = f.result()
            except Exception as e:  # une fiche cassée ne doit pas tout arrêter
                print(f"  ! exception {u}: {e}", file=sys.stderr)
                row = None
            (rows if row else failed).append(row or u)
            if i % 25 == 0:
                print(f"  {i}/{len(urls)}")

    rows.sort(key=lambda r: r["url"])
    with open(OUTPUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["nom", "adresse", "telephone", "metiers", "email", "url"])
        w.writeheader()
        w.writerows(rows)

    with_email = sum(1 for r in rows if r["email"])
    print(f"\nTerminé : {len(rows)} agences -> {OUTPUT} ({with_email} avec email, {len(failed)} échecs)")
    if failed:
        with open("agences_laforet_echecs.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(failed))


if __name__ == "__main__":
    main()
  
