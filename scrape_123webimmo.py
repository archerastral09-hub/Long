#!/usr/bin/env python3
"""Scrape toutes les agences 123webimmo.com -> agences_123webimmo.csv

Récupère par agence : responsable, directeur, adresse, téléphone, email.
Colonnes alignées sur celles de prospection.csv (+ Adresse et Directeur).
"""
import csv
import re
import sys
import time
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.123webimmo.com"
LIST_URL = f"{BASE}/agences"
OUT = "agences_123webimmo.csv"
PAUSE = 1.0  # secondes entre deux requêtes (politesse)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; prospection-bot/1.0)",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
COLUMNS = [
    "Nom de l'agence", "Téléphone de l'agence", "Email de l'agence", "Site web",
    "Indépendant ou Franchise", "Ville / département", "Gérant", "Directeur d'agence",
    "Adresse", "Lien OpinionSystem", "Note OpinionSystem", "Note sur 5", "Nombre d'avis", "Source de l'agence", "Source du gérant", "Date de vérification",
]

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?:\+33|0)\s?[1-9](?:[\s.]?\d{2}){4}")
AGENCY_HREF_RE = re.compile(r"^/agences/(?!baremes)[^/?#]+$")

session = requests.Session()
session.headers.update(HEADERS)


def get_soup(url, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            print(f"  ! {url} (tentative {i + 1}/{retries}) : {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))
    return None


def clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


def list_agency_urls():
    urls = {}  # url -> libellé du lien, ex. "Agence de Grenoble Est (38)"
    soup = get_soup(LIST_URL)
    if soup:
        for a in soup.find_all("a", href=True):
            href = a["href"].replace(BASE, "")
            if AGENCY_HREF_RE.match(href):
                urls.setdefault(urljoin(BASE, href), clean(a.get_text()))
    # Secours : sitemap
    if len(urls) < 5:
        sm = get_soup(f"{BASE}/sitemap.xml")
        if sm:
            for loc in sm.find_all("loc"):
                u = loc.get_text(strip=True)
                if AGENCY_HREF_RE.match(u.replace(BASE, "")):
                    urls.setdefault(u, "")
    return urls


OPINION_RE = re.compile(r"https?://(?:www\.)?opinionsystem\.fr/[^\"'\s<>]*company_id=\d+[^\"'\s<>]*", re.I)


def find_opinionsystem(soup):
    """Lien OpinionSystem : src de l'iframe du bandeau (company_rating.php?company_id=...)."""
    for f in soup.find_all("iframe", src=True):
        if "opinionsystem" in f["src"].lower():
            return f["src"].replace("&amp;", "&")
    # Secours : l'URL peut être injectée par un script ou un autre attribut
    m = OPINION_RE.search(str(soup))
    return m.group(0).replace("&amp;", "&") if m else ""


def parse_opinionsystem_widget(html):
    """Widget OpinionSystem -> (indice de satisfaction en %, note sur 5, nombre d'avis)."""
    note = note5 = nb = ""
    m = re.search(r"Indice de satisfaction\s*:\s*(\d+(?:[.,]\d+)?)\s*%", html, re.I)
    if m:
        pct = float(m.group(1).replace(",", "."))
        note = f"{pct:g}%"
        note5 = f"{pct / 20:.2f}".replace(".", ",")  # 95% -> 4,75
    m = re.search(r"(\d+)\s*Avis", html, re.I)
    if m:
        nb = m.group(1)
    return note, note5, nb


def fetch_opinionsystem(link):
    if not link:
        return "", "", ""
    for i in range(3):
        try:
            r = session.get(link, timeout=30, headers={"Referer": BASE + "/"})
            r.raise_for_status()
            r.encoding = "utf-8"
            return parse_opinionsystem_widget(r.text)
        except requests.RequestException as e:
            print(f"  ! opinionsystem {link} (tentative {i + 1}/3) : {e}", file=sys.stderr)
            time.sleep(2 * (i + 1))
    return "", "", ""


def parse_agency(url, soup, label=""):
    name = label
    d = soup.select_one(".section--agency-details")
    if not d:
        return None

    responsable = directeur = adresse = tel = email = ""
    for block in d.select(".section__info"):
        h3 = block.find("h3")
        label = clean(h3.get_text()).lower() if h3 else ""
        if "responsable" in label:
            for li in block.find_all("li"):
                txt = clean(li.get_text())
                role, _, value = txt.partition(":")
                value = clean(value)
                if not value:
                    continue
                # Sur le site le libellé du responsable peut être vide (": NOM Prénom")
                if "directeur" in role.lower():
                    directeur = value
                elif not responsable:
                    responsable = value
        elif "adresse" in label:
            addr = block.find("address")
            adresse = clean(addr.get_text()) if addr else ""
        elif label == "contact":
            text = block.get_text(" ")
            m = EMAIL_RE.search(text)
            email = m.group(0).lower() if m else ""
            m = PHONE_RE.search(text)
            tel = clean(m.group(0)) if m else ""

    # Nom + département depuis le <title>/h1 : "Agence de Grenoble Est (38)"
    dept = ""
    m = re.search(r"\((\d{2,3}|2[AB])\)", name)
    if m:
        dept = m.group(1)
    if not name:
        name = url.rsplit("/", 1)[-1].replace("-", " ").capitalize()

    cp = re.search(r"\b(\d{2})\s?(\d{3})\b", adresse)
    if cp:
        adresse = adresse.replace(cp.group(0), cp.group(1) + cp.group(2), 1)  # "38 560" -> "38560"
        dept = dept or cp.group(1)

    return {
        "Nom de l'agence": re.sub(r"\s*\(\d+\w?\)\s*$", "", name),
        "Téléphone de l'agence": tel,
        "Email de l'agence": email,
        "Site web": url,
        "Indépendant ou Franchise": "Franchise",
        "Ville / département": dept,
        "Gérant": responsable,
        "Directeur d'agence": directeur,
        "Adresse": adresse,
        "Lien OpinionSystem": find_opinionsystem(soup),
        "Note OpinionSystem": "",
        "Note sur 5": "",
        "Nombre d'avis": "",
        "Source de l'agence": url,
        "Source du gérant": url if responsable else "",
        "Date de vérification": date.today().isoformat(),
    }


def main():
    urls = list_agency_urls()
    print(f"{len(urls)} agences trouvées")
    if not urls:
        sys.exit("Aucune agence trouvée : structure du site modifiée ?")

    rows, failed = [], []
    for i, (url, label) in enumerate(urls.items(), 1):
        soup = get_soup(url)
        row = parse_agency(url, soup, label) if soup else None
        if row:
            if row["Lien OpinionSystem"]:
                time.sleep(PAUSE)
                (row["Note OpinionSystem"], row["Note sur 5"],
                 row["Nombre d'avis"]) = fetch_opinionsystem(row["Lien OpinionSystem"])
            rows.append(row)
            print(f"[{i}/{len(urls)}] {row[COLUMNS[0]]} | {row['Gérant']} | {row[COLUMNS[2]]} | {row['Note sur 5']}/5 ({row["Nombre d'avis"]} avis)")
        else:
            failed.append(url)
            print(f"[{i}/{len(urls)}] ECHEC {url}", file=sys.stderr)
        time.sleep(PAUSE)

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";")
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} agences écrites dans {OUT} ({len(failed)} échecs)")
    if failed:
        print("\n".join(failed), file=sys.stderr)


if __name__ == "__main__":
    main()
