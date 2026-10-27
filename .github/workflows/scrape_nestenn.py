#!/usr/bin/env python3
"""
Scraper Nestenn -> CSV
1. nestenn.com/liste-agences?page=N : toutes les agences
2. page agence : nom, adresse, tel, equipe (nestcards)
3. <site agence>/avis-client : nb avis, note (satisfaction), recommandation, source
4. nestcard du/des responsable(s) : nom, poste, email, tel, portable

Usage:
    pip install requests beautifulsoup4
    python scrape_nestenn.py                 # tout
    python scrape_nestenn.py --limit 5       # test sur 5 agences
Reprise automatique : relancer le script saute les agences deja ecrites.
"""
import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, urlsplit, parse_qs

import requests
from bs4 import BeautifulSoup

BASE = "https://nestenn.com"
LIST_URL = BASE + "/liste-agences"
OUT = "nestenn_agences.csv"
DELAY = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Postes consideres comme "responsable", par ordre de priorite
ROLE_PATTERNS = [
    r"responsable d.?agence",
    r"directeur|directrice",
    r"g[ée]rant|g[ée]rante",
    r"dirigeant|dirigeante",
    r"franchis[ée]",
    r"responsable",
]

COLUMNS = [
    "agence_nom", "agence_adresse", "agence_telephone", "agence_email",
    "agence_url_nestenn", "agence_site",
    "avis_nb", "avis_note_satisfaction_pct", "avis_recommandation_pct", "avis_source",
    "responsable_nom", "responsable_poste", "responsable_email",
    "responsable_tel", "responsable_portable", "responsable_nestcard",
]

session = requests.Session()
session.headers.update(HEADERS)


def get(url, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return BeautifulSoup(r.text, "html.parser")
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- liste
def parse_list_page(soup):
    """Retourne [{url, nom, email, base}] dans l'ordre du document."""
    agencies, cur = [], None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/liste-agences/agence-immobiliere-" in href:
            url = urljoin(BASE, href)
            txt = clean(a.get_text(" "))
            if cur is None or cur["url"] != url:
                cur = {"url": url, "nom": "", "email": "", "base": ""}
                agencies.append(cur)
            if txt and not cur["nom"]:
                cur["nom"] = re.sub(r"^Immobilier\s+", "", txt)
        elif cur is not None:
            if "contact-autre?mail=" in href and not cur["email"]:
                q = parse_qs(urlsplit(href).query)
                cur["email"] = (q.get("mail") or [""])[0]
            elif "/achat-immobilier" in href and not cur["base"]:
                u = urlsplit(href)
                cur["base"] = f"{u.scheme}://{u.netloc}"
    return agencies


def collect_agencies(limit=None):
    seen, out, page = set(), [], 1
    while True:
        soup = get(f"{LIST_URL}?page={page}")
        if soup is None:
            break
        new = [a for a in parse_list_page(soup) if a["url"] not in seen]
        if not new:
            break
        for a in new:
            seen.add(a["url"])
            out.append(a)
        print(f"[liste] page {page}: +{len(new)} (total {len(out)})", flush=True)
        if limit and len(out) >= limit:
            return out[:limit]
        page += 1
        time.sleep(DELAY)
    return out


# ---------------------------------------------------------------- agence
def parse_agency_page(soup, base_hint):
    info = {"adresse": "", "tel": "", "site": base_hint, "team": []}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "google.com/maps" in h and not info["adresse"]:
            info["adresse"] = clean(a.get_text(" "))
        elif h.startswith("tel:") and not info["tel"]:
            info["tel"] = clean(h[4:])
        elif re.search(r"nestcard-\d+", h):
            title = a.get("title", "")
            m = re.match(r"Nestcard de (.+)", title)
            name = clean(m.group(1)) if m else ""
            info["team"].append({
                "name": name,
                "text": clean(a.get_text(" ")),
                "url": urljoin(base_hint + "/", h),
            })
        elif "Site immobilier" in a.get_text() and h.startswith("http"):
            u = urlsplit(h)
            info["site"] = f"{u.scheme}://{u.netloc}"
    return info


def pick_responsables(team):
    """Membres dont le poste correspond a un role de direction (priorite aux roles les plus precis)."""
    for pat in ROLE_PATTERNS:
        hits = [m for m in team if re.search(pat, m["text"], re.I)]
        if hits:
            # on garde tous ceux du meme niveau de priorite
            return hits
    return []


# ---------------------------------------------------------------- avis
def parse_reviews(base):
    res = {"nb": "", "note": "", "reco": "", "source": ""}
    soup = get(f"{base}/avis-client")
    if soup is None:
        return res
    text = clean(soup.get_text(" "))
    m = re.search(r"(\d[\d\s]*)\s*avis clients", text)
    if m:
        res["nb"] = m.group(1).replace(" ", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*satisfaction", text, re.I)
    if m:
        res["note"] = m.group(1).replace(",", ".")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*recommandation", text, re.I)
    if m:
        res["reco"] = m.group(1).replace(",", ".")
    if re.search(r"opinion\s?system", text, re.I):
        res["source"] = "Opinion System (ISO 20252, AFNOR)"
    return res


# ---------------------------------------------------------------- nestcard
def parse_nestcard(url):
    out = {"nom": "", "poste": "", "email": "", "tel": "", "portable": ""}
    soup = get(url)
    if soup is None:
        return out
    # 1) lien vCard : donnees structurees
    for a in soup.find_all("a", href=True):
        if "vCard_generator.php" in a["href"]:
            q = parse_qs(urlsplit(a["href"]).query)
            g = lambda k: clean((q.get(k) or [""])[0])
            out["nom"] = clean(f"{g('first_name')} {g('last_name')}")
            out["poste"] = g("function")
            out["email"] = g("mail")
            out["tel"] = g("telephone")
            out["portable"] = g("telephone_portable")
            break
    # 2) repli sur le texte visible
    if not out["nom"]:
        h1 = soup.find("h1")
        out["nom"] = clean(h1.get_text(" ")) if h1 else ""
    if not out["email"]:
        a = soup.find("a", href=re.compile(r"^mailto:"))
        if a:
            out["email"] = a["href"][7:].split("?")[0]
    if not out["tel"] or not out["portable"]:
        tels = [clean(a["href"][4:]) for a in soup.find_all("a", href=re.compile(r"^tel:"))]
        if tels and not out["tel"]:
            out["tel"] = tels[0]
        if len(tels) > 1 and not out["portable"]:
            out["portable"] = tels[1]
    return out


# ---------------------------------------------------------------- main
def already_done():
    if not os.path.exists(OUT):
        return set()
    with open(OUT, newline="", encoding="utf-8-sig") as f:
        return {r["agence_url_nestenn"] for r in csv.DictReader(f, delimiter=";")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    agencies = collect_agencies(args.limit)
    print(f"{len(agencies)} agences trouvees", flush=True)

    done = already_done()
    new_file = not os.path.exists(OUT)
    reviews_cache = {}

    with open(OUT, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        if new_file:
            w.writeheader()

        for i, ag in enumerate(agencies, 1):
            if ag["url"] in done:
                continue
            print(f"[{i}/{len(agencies)}] {ag['nom']}", flush=True)

            soup = get(ag["url"])
            time.sleep(DELAY)
            info = parse_agency_page(soup, ag["base"]) if soup else \
                {"adresse": "", "tel": "", "site": ag["base"], "team": []}
            base = info["site"] or ag["base"]

            # avis (cache : certains sites sont partages entre plusieurs fiches)
            if base and base not in reviews_cache:
                reviews_cache[base] = parse_reviews(base)
                time.sleep(DELAY)
            rv = reviews_cache.get(base, {"nb": "", "note": "", "reco": "", "source": ""})

            row_base = {
                "agence_nom": ag["nom"],
                "agence_adresse": info["adresse"],
                "agence_telephone": info["tel"],
                "agence_email": ag["email"],
                "agence_url_nestenn": ag["url"],
                "agence_site": base,
                "avis_nb": rv["nb"],
                "avis_note_satisfaction_pct": rv["note"],
                "avis_recommandation_pct": rv["reco"],
                "avis_source": rv["source"],
            }

            resp = pick_responsables(info["team"])
            if not resp:
                w.writerow({**row_base, **{c: "" for c in COLUMNS if c.startswith("responsable_")}})
            for m in resp:
                nc = parse_nestcard(m["url"])
                time.sleep(DELAY)
                w.writerow({
                    **row_base,
                    "responsable_nom": nc["nom"] or m["name"],
                    "responsable_poste": nc["poste"] or m["text"],
                    "responsable_email": nc["email"],
                    "responsable_tel": nc["tel"],
                    "responsable_portable": nc["portable"],
                    "responsable_nestcard": m["url"],
                })
            f.flush()

    print(f"Termine -> {OUT}")


if __name__ == "__main__":
    sys.exit(main())
