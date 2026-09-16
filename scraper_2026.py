import os
import re
import time
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

BASE_URL = "https://vyhledavac.nssoud.cz"
OUTPUT_DIR = "judikatura/Afs/2026"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}

def sanitize_filename(spzn: str) -> str:
    clean = re.sub(r"[^\w\s-]", "_", spzn)
    clean = re.sub(r"\s+", "_", clean.strip())
    return f"NSS_{clean}.md"

def extract_content(html: str, doc_url: str):
    soup = BeautifulSoup(html, "html.parser")
    
    def get_val(element_id):
        el = soup.find(id=element_id)
        return el.get_text(strip=True) if el else ""

    spzn = get_val("lblSpisovaZnacka") or "Neznama_znacka"
    ecli = get_val("lblEcli")
    datum = get_val("lblDatumRozhodnuti")
    forma = get_val("lblFormaRozhodnuti")

    content_div = soup.find("div", id="divTextRozhodnuti") or soup.find("div", class_="rozhodnuti-fulltext")
    body_html = str(content_div) if content_div else str(soup.body)
    markdown_text = md(body_html, heading_style="ATX", strip=['script', 'style'])

    yaml_frontmatter = f"""---
soud: Nejvyšší správní soud
rejstrik: Afs
spisova_znacka: "{spzn}"
ecli: "{ecli}"
datum_rozhodnuti: "{datum}"
forma: "{forma}"
zdroj_url: "{doc_url}"
---

"""
    return yaml_frontmatter + markdown_text.strip(), spzn

def run():
    print("--- 1. Načítám hlavní stránku vyhledávače NSS ---")
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        res = client.get(BASE_URL)
        if res.status_code != 200:
            print(f"Nelze načíst NSS, stav: {res.status_code}")
            return

        soup = BeautifulSoup(res.text, "html.parser")
        form = soup.find("form")
        if not form:
            print("Formulář nebyl nalezen.")
            return

        # Sestavíme URL, kam se formulář posílá
        action_url = urljoin(BASE_URL, form.get("action", ""))
        method = form.get("method", "post").lower()
        print(f"Formulář nalezen. Cíl: {action_url} (metoda: {method.upper()})")

        # 2. Posbíráme všechna pole formuláře (včetně skrytých ViewState/CSRF tokenů)
        form_data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                form_data[name] = inp.get("value", "")

        for sel in form.find_all("select"):
            name = sel.get("name")
            if name:
                selected = sel.find("option", selected=True)
                form_data[name] = selected.get("value", "") if selected else ""

        print(f"Nalezená pole formuláře: {list(form_data.keys())}")

        # 3. Nastavíme parametry pro daňový senát Afs a rok 2026
        # Skript prohledá názvy polí a dosadí hodnoty
        for key in list(form_data.keys()):
            k_low = key.lower()
            if "rejstrik" in k_low or "senat" in k_low:
                form_data[key] = "Afs"
            elif "rok" in k_low:
                form_data[key] = "2026"

        print("--- 2. Odesílám vyhledávací dotaz (Afs 2026) ---")
        if method == "post":
            search_res = client.post(action_url, data=form_data)
        else:
            search_res = client.get(action_url, params=form_data)

        print(f"Výsledek hledání HTTP: {search_res.status_code}")
        search_soup = BeautifulSoup(search_res.text, "html.parser")

        # Hledáme odkazy na texty rozhodnutí
        links = search_soup.find_all("a", href=re.compile(r"/DokumentOriginal/Text/|/Text/"))
        print(f"Nalezeno {len(links)} rozhodnutí.")

        if not links:
            # Pro kontrolu vypíšeme text, který stránka vrátila
            text_snippet = search_soup.get_text(separator=" ", strip=True)[:400]
            print(f"Ukázka textu z odpovědi: {text_snippet}")
            return

        # 3. Stažení nalezených rozhodnutí
        print(f"--- 3. Zahajuji stahování {len(links)} souborů ---")
        saved_count = 0
        for link in links:
            href = link["href"]
            doc_url = urljoin(BASE_URL, href)
            try:
                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    md_text, spzn = extract_content(doc_res.text, doc_url)
                    filename = sanitize_filename(spzn)
                    filepath = os.path.join(OUTPUT_DIR, filename)

                    if not os.path.exists(filepath):
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(md_text)
                        print(f"Uloženo: {filename}")
                        saved_count += 1
                    else:
                        print(f"Již existuje: {filename}")
                time.sleep(1.0)
            except Exception as e:
                print(f"Chyba u {doc_url}: {e}")

        print(f"Dokončeno. Nově uloženo {saved_count} rozhodnutí.")

if __name__ == "__main__":
    run()
