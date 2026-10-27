#!/usr/bin/env python3
"""Récupère toutes les agences l'Adresse (nom, adresse, téléphone) -> agences.csv / agences.json

Stratégie :
 1. /trouver-votre-agence  -> liens vers les pages région / ville (/recherche/agence/...) et les agences
 2. chaque page région/ville -> cartes d'agences (div.agence) + liens /agence/<slug>/<id>
 3. chaque page /agence/<slug>/<id> -> nom, adresse, téléphone (source la plus fiable)
"""
import csv, json, re, sys, time
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

BASE = "https://www.ladresse.com"
START = f"{BASE}/trouver-votre-agence"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept-Language": "fr-FR,fr;q=0.9"}
DELAY = 0.7  # politesse envers le serveur

s = requests.Session()
s.headers.update(HEADERS)


def get(url, tries=3):
    for i in range(tries):
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 200:
                r.encoding = "utf-8"
                return r.text
            print(f"  HTTP {r.status_code} {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  erreur {e} {url}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return None


AGENCE_RE = re.compile(r"/agence/[^/]+/(\d+)/?$")


def clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


def fmt_tel(t):
    d = re.sub(r"\D", "", t or "")
    return " ".join(d[i:i + 2] for i in range(0, len(d), 2)) if len(d) == 10 else clean(t)


def collect_links(html):
    soup = BeautifulSoup(html, "html.parser")
    agences, pages = {}, set()
    for a in soup.find_all("a", href=True):
        u = urljoin(BASE, a["href"]).split("#")[0]
        m = AGENCE_RE.search(u)
        if m:
            agences[m.group(1)] = u
        elif "/recherche/agence/" in u:
            pages.add(u)
    return agences, pages


def parse_agence_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    nom = adresse = tel = ""
    h2 = soup.select_one(".contenu-article h2")
    # bloc "Contactez notre agence"
    carte = soup.select_one(".carte-adresse")
    if carte:
        titre = carte.select_one(".carte-titre")
        nom = clean(titre.get_text(" ")) if titre else ""
        contenu = carte.select_one(".carte-contenu")
        if contenu:
            lignes = []
            for node in contenu.children:
                if getattr(node, "name", None) == "br":
                    continue
                if getattr(node, "name", None) == "a":
                    if node.get("href", "").startswith("tel:") and not tel:
                        tel = fmt_tel(node.get_text())
                    continue
                if getattr(node, "name", None) in ("b", "div"):
                    break  # début des horaires
                t = clean(str(node) if isinstance(node, str) else node.get_text(" "))
                if t:
                    lignes.append(t)
            adresse = ", ".join(lignes)
    if not tel:
        a = soup.select_one('a[href^="tel:"]')
        if a:
            tel = fmt_tel(a["href"][4:])
    if not adresse:
        i = soup.select_one(".fa-house")
        if i and i.parent:
            adresse = clean(i.parent.get_text(" "))
    if not nom:
        h1 = soup.find("h1")
        nom = clean(h1.get_text(" ")) if h1 else clean(h2.get_text(" ")) if h2 else ""
    m = AGENCE_RE.search(url)
    return {"id": m.group(1) if m else "", "nom": nom, "adresse": adresse, "telephone": tel, "url": url}


def parse_cards(html):
    """Cartes de liste (div.agence) : sert de secours si la page agence échoue."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for c in soup.select("div.agence"):
        a = c.find("a", href=AGENCE_RE)
        if not a:
            continue
        url = urljoin(BASE, a["href"])
        nom = c.select_one(".agence-nom")
        desc = c.select_one(".agence-description")
        tel = ""
        lignes = []
        if desc:
            t = desc.find("a", href=re.compile("^tel:"))
            tel = fmt_tel(t.get_text()) if t else ""
            for n in desc.children:
                if getattr(n, "name", None) in ("br", "a"):
                    continue
                x = clean(str(n) if isinstance(n, str) else n.get_text(" "))
                if x:
                    lignes.append(x)
        out[AGENCE_RE.search(url).group(1)] = {
            "id": AGENCE_RE.search(url).group(1),
            "nom": clean(nom.get_text(" ")) if nom else "",
            "adresse": ", ".join(lignes), "telephone": tel, "url": url}
    return out


def main():
    html = get(START)
    if not html:
        sys.exit("Impossible de charger la page de départ")
    agences, pages = collect_links(html)
    cards = parse_cards(html)

    seen = set()
    for p in sorted(pages):
        if p in seen:
            continue
        seen.add(p)
        time.sleep(DELAY)
        h = get(p)
        if not h:
            continue
        a2, p2 = collect_links(h)
        agences.update(a2)
        cards.update(parse_cards(h))
        pages |= p2  # pages ville découvertes depuis une région
    print(f"{len(agences)} agences trouvées ({len(seen)} pages région/ville parcourues)")

    rows = []
    for aid, url in sorted(agences.items(), key=lambda x: int(x[0])):
        time.sleep(DELAY)
        h = get(url)
        row = parse_agence_page(h, url) if h else None
        fb = cards.get(aid, {})
        if not row:
            row = fb
        else:
            row["nom"] = fb.get("nom") or row["nom"]  # nom court de la carte
            for k in ("adresse", "telephone"):
                row[k] = row[k] or fb.get(k, "")
        if row:
            rows.append(row)
            print(f"  {row['nom']} | {row['adresse']} | {row['telephone']}")

    if not rows:
        sys.exit("Aucune agence récupérée")
    with open("agences.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["nom", "adresse", "telephone", "url", "id"], delimiter=";")
        w.writeheader()
        w.writerows(rows)
    with open("agences.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"OK : {len(rows)} agences -> agences.csv / agences.json")


if __name__ == "__main__":
    main()
