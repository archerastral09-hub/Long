#!/usr/bin/env python3
"""
Scraper Nestenn -> CSV  (v2)
1. nestenn.com/liste-agences?page=N : toutes les agences (~405, 9 pages)
2. page agence : adresse, tel, equipe (nestcards) + note/nb d'avis DE L'AGENCE
3. <site agence>/avis-client : recommandation (+ note/nb si absents de la page agence)
4. nestcard du/des responsable(s) : nom, poste, email, tel, portable

Usage:
    pip install requests beautifulsoup4
    python scrape_nestenn.py                  # tout (reprend si le CSV existe)
    python scrape_nestenn.py --limit 5        # test sur 5 agences
    python scrape_nestenn.py --fresh          # repart de zero (efface le CSV)
    python scrape_nestenn.py --debug-nestcard https://immobilier-agen.nestenn.com/nestcard-547166

Corrections v2 :
 - plus de repli sur le <h1> (c'etait le slogan de l'agence, pas la personne)
 - nom/poste de repli depuis le <title> de la nestcard
 - email de repli par regex, tel/portable de repli par libelle "Tel"/"Portable"
 - portable vide s'il est identique au tel (c'etait le numero de l'agence en double)
 - detection des redirections (une nestcard qui renvoie vers une autre page est ignoree + comptee)
 - avis : on ne lit plus le pied de page "reseau" (94 % / 67 508 clients), mais le bloc de l'agence
 - resume final avec taux de remplissage pour voir tout de suite si le scrape est sain
"""
import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import urljoin, urlsplit, parse_qs

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
    "Accept": "text/html,application/xhtml+xml",
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

PHONE_RE = r"0\d(?:[\s.]?\d{2}){4}"
EMAIL_RE = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
NB_RE = r"(\d+(?:[\s\u00a0\u202f]\d{3})*)"
PCT_RE = r"(\d+(?:[.,]\d+)?)"

STATS = {"nestcards": 0, "redirects": 0, "emails": 0, "agences": 0, "avec_resp": 0}

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------- http
def fetch(url, retries=3):
    """Retourne (soup, url_finale). soup = None si echec/404."""
    for i in range(retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return BeautifulSoup(r.text, "html.parser"), r.url
            if r.status_code in (404, 410):
                return None, url
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    return None, url


def get(url, retries=3):
    return fetch(url, retries)[0]


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 10:
        return " ".join(digits[i:i + 2] for i in range(0, 10, 2))
    return clean(s)


def to_num(s):
    return (s or "").replace(",", ".").replace("\u00a0", "").replace("\u202f", "").replace(" ", "")


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
    if not limit and len(out) < 350:
        print(f"! seulement {len(out)} agences (attendu ~405) : la liste a peut-etre change",
              file=sys.stderr)
    return out


# ---------------------------------------------------------------- avis
def cut_footer(text):
    """Coupe le pied de page 'L'ensemble de nos agences ... 67508 clients' (stats du RESEAU)."""
    return re.split(r"ensemble de nos agences", text, maxsplit=1, flags=re.I)[0]


def parse_review_stats(soup):
    """Extrait nb / note / reco du bloc de l'agence (hors pied de page reseau)."""
    res = {"nb": "", "note": "", "reco": "", "source": ""}
    if soup is None:
        return res
    full = clean(soup.get_text(" "))
    text = cut_footer(full)

    # bloc "96.9% 170 avis clients*" (page agence nestenn.com)
    m = re.search(PCT_RE + r"\s*%\s*" + NB_RE + r"\s*avis\s+clients", text, re.I)
    if m:
        res["note"] = to_num(m.group(1))
        res["nb"] = to_num(m.group(2))
    if not res["note"]:
        m = re.search(PCT_RE + r"\s*%\s*(?:de\s+)?satisfaction", text, re.I)
        if m:
            res["note"] = to_num(m.group(1))
    if not res["nb"]:
        m = re.search(NB_RE + r"\s*avis\s+clients", text, re.I)
        if m:
            res["nb"] = to_num(m.group(1))
    m = re.search(PCT_RE + r"\s*%\s*(?:de\s+)?recommandation", text, re.I)
    if m:
        res["reco"] = to_num(m.group(1))
    if re.search(r"opinion\s?system", full, re.I):
        res["source"] = "Opinion System (ISO 20252, AFNOR)"
    return res


# ---------------------------------------------------------------- agence
def parse_agency_page(soup, base_hint):
    info = {"adresse": "", "tel": "", "site": base_hint, "team": [], "avis": {}}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "google.com/maps" in h and not info["adresse"]:
            info["adresse"] = clean(a.get_text(" "))
        elif h.startswith("tel:") and not info["tel"]:
            info["tel"] = norm_phone(h[4:])
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
    if not info["tel"]:
        m = re.search(PHONE_RE, cut_footer(clean(soup.get_text(" "))))
        if m:
            info["tel"] = norm_phone(m.group(0))
    info["avis"] = parse_review_stats(soup)
    return info


def pick_responsables(team):
    """Membres dont le poste correspond a un role de direction (roles les plus precis d'abord)."""
    for pat in ROLE_PATTERNS:
        hits = [m for m in team if re.search(pat, m["text"], re.I)]
        if hits:
            return hits
    return []


# ---------------------------------------------------------------- nestcard
def parse_nestcard_soup(soup, final_url):
    out = {"nom": "", "poste": "", "email": "", "tel": "", "portable": ""}
    if soup is None:
        return out
    if "nestcard-" not in final_url:
        print(f"  ! nestcard redirigee vers {final_url} -> ignoree", file=sys.stderr, flush=True)
        STATS["redirects"] += 1
        return out

    # 1) lien vCard : donnees structurees (source la plus fiable)
    for a in soup.find_all("a", href=True):
        if "vCard_generator" in a["href"]:
            q = parse_qs(urlsplit(a["href"]).query)
            g = lambda k: clean((q.get(k) or [""])[0])
            out["nom"] = clean(f"{g('first_name')} {g('last_name')}")
            out["poste"] = g("function")
            out["email"] = g("mail")
            out["tel"] = g("telephone")
            out["portable"] = g("telephone_portable")
            break

    # 2) repli : <title> = "Prenom NOM - Poste immobilier a Ville | Nestenn"
    if not out["nom"] or not out["poste"]:
        title = clean(soup.title.get_text(" ")) if soup.title else ""
        m = re.match(r"(.+?)\s+-\s+(.+?)\s+immobilier\s+à\s+", title) \
            or re.match(r"(.+?)\s+-\s+(.+?)\s*\|", title)
        if m:
            out["nom"] = out["nom"] or clean(m.group(1))
            out["poste"] = out["poste"] or clean(m.group(2))

    text = clean(soup.get_text(" "))

    # 3) repli email
    if not out["email"]:
        a = soup.find("a", href=re.compile(r"^mailto:", re.I))
        if a:
            out["email"] = a["href"][7:].split("?")[0].strip()
        else:
            m = re.search(EMAIL_RE, text)
            if m:
                out["email"] = m.group(0)

    # 4) repli tel / portable par libelle
    if not out["tel"]:
        m = re.search(r"\bT[ée]l\.?\s*:?\s*(" + PHONE_RE + ")", text)
        if m:
            out["tel"] = m.group(1)
    if not out["portable"]:
        m = re.search(r"(?:Portable|Mobile)\s*:?\s*(" + PHONE_RE + ")", text, re.I)
        if m:
            out["portable"] = m.group(1)

    out["tel"] = norm_phone(out["tel"])
    out["portable"] = norm_phone(out["portable"])
    if out["portable"] and out["portable"] == out["tel"]:
        out["portable"] = ""          # doublon du numero d'agence : pas un vrai portable
    return out


def parse_nestcard(url):
    soup, final = fetch(url)
    return parse_nestcard_soup(soup, final)


# ---------------------------------------------------------------- debug
def debug_nestcard(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    print("status      :", r.status_code)
    print("url finale  :", r.url)
    print("redirections:", [h.url for h in r.history])
    print("taille HTML :", len(r.text))
    print("vCard_generator present :", "vCard_generator" in r.text)
    print("@nestenn.com present    :", "@nestenn.com" in r.text)
    soup = BeautifulSoup(r.text, "html.parser")
    print("<title>     :", clean(soup.title.get_text()) if soup.title else None)
    print("h1          :", [clean(h.get_text(" ")) for h in soup.find_all("h1")])
    print("parse       :", parse_nestcard_soup(soup, r.url))


# ---------------------------------------------------------------- main
def already_done(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["agence_url_nestenn"] for r in csv.DictReader(f, delimiter=";")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--fresh", action="store_true", help="efface le CSV et repart de zero")
    ap.add_argument("--debug-nestcard", metavar="URL", help="diagnostique une nestcard et quitte")
    args = ap.parse_args()

    if args.debug_nestcard:
        debug_nestcard(args.debug_nestcard)
        return 0

    if args.fresh and os.path.exists(args.out):
        os.remove(args.out)

    agencies = collect_agencies(args.limit)
    print(f"{len(agencies)} agences trouvees", flush=True)

    done = already_done(args.out)
    new_file = not os.path.exists(args.out)
    reviews_cache = {}

    with open(args.out, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        if new_file:
            w.writeheader()

        for i, ag in enumerate(agencies, 1):
            if ag["url"] in done:
                continue
            print(f"[{i}/{len(agencies)}] {ag['nom']}", flush=True)

            soup, _ = fetch(ag["url"])
            time.sleep(DELAY)
            info = parse_agency_page(soup, ag["base"]) if soup else \
                {"adresse": "", "tel": "", "site": ag["base"], "team": [], "avis": {}}
            base = info["site"] or ag["base"]

            # avis : valeurs de la page agence, completees par /avis-client (cache par site)
            av = {"nb": "", "note": "", "reco": "", "source": ""}
            av.update({k: v for k, v in (info["avis"] or {}).items() if v})
            if base:
                if base not in reviews_cache:
                    reviews_cache[base] = parse_review_stats(get(f"{base}/avis-client"))
                    time.sleep(DELAY)
                for k, v in reviews_cache[base].items():
                    if v and not av.get(k):
                        av[k] = v

            row_base = {
                "agence_nom": ag["nom"],
                "agence_adresse": info["adresse"],
                "agence_telephone": info["tel"],
                "agence_email": ag["email"],
                "agence_url_nestenn": ag["url"],
                "agence_site": base,
                "avis_nb": av["nb"],
                "avis_note_satisfaction_pct": av["note"],
                "avis_recommandation_pct": av["reco"],
                "avis_source": av["source"],
            }

            STATS["agences"] += 1
            resp = pick_responsables(info["team"])
            if not resp:
                w.writerow({**row_base, **{c: "" for c in COLUMNS if c.startswith("responsable_")}})
            else:
                STATS["avec_resp"] += 1
            for m in resp:
                nc = parse_nestcard(m["url"])
                time.sleep(DELAY)
                STATS["nestcards"] += 1
                if nc["email"]:
                    STATS["emails"] += 1
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

    s = STATS
    print(f"Termine -> {args.out}")
    print(f"Resume : {s['agences']} agences traitees, {s['avec_resp']} avec responsable, "
          f"{s['nestcards']} nestcards, {s['emails']} emails responsables, "
          f"{s['redirects']} nestcards redirigees")
    if s["nestcards"] and s["emails"] / s["nestcards"] < 0.5:
        print("! moins de 50 % d'emails : lance --debug-nestcard sur une nestcard", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
            
