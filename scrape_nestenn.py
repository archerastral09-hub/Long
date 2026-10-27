#!/usr/bin/env python3
"""
Scrape des agences Nestenn (https://nestenn.com/liste-agences).

Pipeline :
  1. Pages liste (?page=1..N)  -> nom, adresse, tél agence, e-mail agence, URL page agence
  2. Page agence               -> nb d'avis, note de satisfaction, liste des agents (poste + nestcard)
  3. Nestcard du/des responsables -> nom complet, poste, e-mail PERSO du responsable, fixe, mobile

Sortie (dossier OUT_DIR, défaut "output") :
  - agences_nestenn.csv : 1 ligne par responsable (champs agence répétés)
  - agents_nestenn.csv  : tous les agents (seulement si ALL_AGENTS=1)

Variables d'environnement : WORKERS, DELAY, MAX_AGENCIES, ALL_AGENTS, OUT_DIR
"""
import csv
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://nestenn.com"
LIST_URL = BASE + "/liste-agences"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

WORKERS = int(os.getenv("WORKERS", "4"))
DELAY = float(os.getenv("DELAY", "0.3"))
MAX_AGENCIES = int(os.getenv("MAX_AGENCIES", "0"))  # 0 = toutes
ALL_AGENTS = os.getenv("ALL_AGENTS", "0") == "1"
OUT_DIR = os.getenv("OUT_DIR", "output")

# Postes considérés comme "gérant / responsable"
RESP_RE = re.compile(
    r"responsable|g[ée]rant|directeur|directrice|dirigeant|franchis|titulaire|manager",
    re.I,
)

_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
        _local.s = s
    return _local.s


def fetch(url, retries=4):
    """GET avec retries/backoff. Retourne le HTML (str) ou None."""
    for i in range(retries):
        try:
            time.sleep(DELAY)
            r = session().get(url, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429, 500, 502, 503, 504):
                time.sleep(2 ** i * 2)
                continue
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as e:
            print(f"  ! {url} ({e})", file=sys.stderr)
            time.sleep(2 ** i * 2)
    return None


def clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


# --------------------------------------------------------------------------
# 1. Liste des agences
# --------------------------------------------------------------------------
def parse_list(html):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for h3 in soup.select("h3.titre2"):
        a = h3.find("a", href=True)
        card = h3.find_parent("div", class_="white_bg")
        if not a or not card or "/liste-agences/" not in a["href"]:
            continue
        addr = card.find("p")
        email = ""
        mail_a = card.find("a", href=re.compile(r"[?&]mail="))
        if mail_a:
            email = (parse_qs(urlparse(mail_a["href"]).query).get("mail") or [""])[0].strip()
        tel_a = card.find("a", href=re.compile(r"^tel:"))
        tel = clean(tel_a["href"][4:]) if tel_a else ""
        site = ""
        biens = card.find("a", title=re.compile(r"^Voir les biens", re.I))
        if biens:
            u = urlparse(biens["href"])
            site = f"{u.scheme}://{u.netloc}"
        out.append(
            {
                "nom": clean(h3.get_text()),
                "adresse": clean(addr.get_text()) if addr else "",
                "telephone_agence": tel,
                "email_agence": email,
                "site_agence": site,
                "url_agence": a["href"],
            }
        )
    return out


def max_page(html):
    nums = [int(n) for n in re.findall(r"liste-agences\?page=(\d+)", html)]
    return max(nums) if nums else 1


def get_all_agencies():
    first = fetch(LIST_URL + "?page=1")
    if not first:
        sys.exit("Impossible de charger la liste des agences")
    n = max_page(first)
    print(f"{n} pages de liste")
    agencies = {a["url_agence"]: a for a in parse_list(first)}
    for p in range(2, n + 1):
        html = fetch(f"{LIST_URL}?page={p}")
        if not html:
            print(f"  ! page {p} vide", file=sys.stderr)
            continue
        for a in parse_list(html):
            agencies.setdefault(a["url_agence"], a)
    print(f"{len(agencies)} agences")
    return list(agencies.values())


# --------------------------------------------------------------------------
# 2. Page agence
# --------------------------------------------------------------------------
def parse_agency(html):
    soup = BeautifulSoup(html, "lxml")
    info = {"nb_avis": "", "note_satisfaction": "", "note_sur_5": "", "agents": []}

    nb = soup.select_one(".nb_avis2")
    if nb:
        m = re.search(r"(\d[\d\s]*)\s*avis", nb.get_text(" "))
        if m:
            info["nb_avis"] = re.sub(r"\s", "", m.group(1))

    pct = soup.select_one(".pourcentage")
    if pct:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", pct.get_text())
        if m:
            v = m.group(1).replace(",", ".")
            info["note_satisfaction"] = v + "%"
            info["note_sur_5"] = str(round(float(v) / 20, 2))

    seen = set()
    for a in soup.find_all("a", href=re.compile(r"/nestcard-\d+")):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)
        divs = [clean(d.get_text()) for d in a.find_all("div") if not d.find("div")]
        divs = [d for d in divs if d and not d.upper().startswith("CLUB")]
        poste = divs[-1] if len(divs) >= 2 else ""
        title = a.get("title", "")
        full = re.sub(r"^Nestcard de\s*", "", title).strip() or (divs[0] if divs else "")
        info["agents"].append({"nom": full, "poste": poste, "url": href})
    return info


# --------------------------------------------------------------------------
# 3. Nestcard
# --------------------------------------------------------------------------
def parse_nestcard(html):
    soup = BeautifulSoup(html, "lxml")
    d = {"nom": "", "poste": "", "email": "", "telephone": "", "mobile": ""}
    h1 = soup.find("h1")
    if h1:
        d["nom"] = clean(h1.get_text(" "))
        poste = h1.find_next_sibling("div")
        if poste:
            d["poste"] = clean(poste.get_text())
    for lab in soup.find_all("div", class_="w20"):
        key = clean(lab.get_text()).lower()
        val = lab.find_next_sibling("div")
        if not val:
            continue
        a = val.find("a", href=True)
        if key == "mail":
            d["email"] = (a["href"].replace("mailto:", "").strip() if a else clean(val.get_text()))
        elif key == "tel":
            d["telephone"] = clean(val.get_text())
        elif key == "portable":
            d["mobile"] = clean(val.get_text())
    return d


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
FIELDS = [
    "nom",
    "adresse",
    "telephone_agence",
    "email_agence",
    "responsable",
    "poste",
    "email_responsable",
    "telephone_responsable",
    "mobile_responsable",
    "nb_avis",
    "note_satisfaction",
    "note_sur_5",
    "site_agence",
    "sources",
]
AGENT_FIELDS = ["agence", "nom", "poste", "url_nestcard", "email", "telephone", "mobile"]


def process(agency):
    base = {k: agency.get(k, "") for k in ("nom", "adresse", "telephone_agence", "email_agence", "site_agence")}
    base.update({"nb_avis": "", "note_satisfaction": "", "note_sur_5": ""})
    sources = [agency["url_agence"]]
    rows, agent_rows = [], []

    html = fetch(agency["url_agence"])
    info = parse_agency(html) if html else {"agents": []}
    for k in ("nb_avis", "note_satisfaction", "note_sur_5"):
        base[k] = info.get(k, "")

    agents = info["agents"]
    resps = [a for a in agents if RESP_RE.search(a["poste"])]
    if not resps and agents:
        resps = agents[:1]  # fallback : premier agent listé

    cards = {}
    to_fetch = agents if ALL_AGENTS else resps
    for a in to_fetch:
        h = fetch(a["url"])
        cards[a["url"]] = parse_nestcard(h) if h else {}

    for a in resps:
        c = cards.get(a["url"], {})
        rows.append(
            {
                **base,
                "responsable": c.get("nom") or a["nom"],
                "poste": c.get("poste") or a["poste"],
                "email_responsable": c.get("email", ""),
                "telephone_responsable": c.get("telephone", ""),
                "mobile_responsable": c.get("mobile", ""),
                "sources": " | ".join(sources + [a["url"]]),
            }
        )
    if not rows:  # agence sans agent trouvé
        rows.append(
            {**base, "responsable": "", "poste": "", "email_responsable": "",
             "telephone_responsable": "", "mobile_responsable": "", "sources": " | ".join(sources)}
        )

    if ALL_AGENTS:
        for a in agents:
            c = cards.get(a["url"], {})
            agent_rows.append(
                {
                    "agence": agency["nom"],
                    "nom": c.get("nom") or a["nom"],
                    "poste": c.get("poste") or a["poste"],
                    "url_nestcard": a["url"],
                    "email": c.get("email", ""),
                    "telephone": c.get("telephone", ""),
                    "mobile": c.get("mobile", ""),
                }
            )
    return rows, agent_rows


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    agencies = get_all_agencies()
    if MAX_AGENCIES:
        agencies = agencies[:MAX_AGENCIES]

    all_rows, all_agents = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, (rows, agents) in enumerate(ex.map(process, agencies), 1):
            all_rows += rows
            all_agents += agents
            if i % 25 == 0:
                print(f"{i}/{len(agencies)}")

    write_csv(os.path.join(OUT_DIR, "agences_nestenn.csv"), FIELDS, all_rows)
    if ALL_AGENTS:
        write_csv(os.path.join(OUT_DIR, "agents_nestenn.csv"), AGENT_FIELDS, all_agents)

    n_mail_ag = sum(1 for r in all_rows if r["email_agence"])
    n_mail_resp = sum(1 for r in all_rows if r["email_responsable"])
    print(f"OK : {len(all_rows)} lignes | e-mail agence: {n_mail_ag} | e-mail responsable: {n_mail_resp}")


if __name__ == "__main__":
    main()
