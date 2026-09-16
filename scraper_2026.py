import os
import re
import time
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

BASE_URL = "https://vyhledavac.nssoud.cz"
SEARCH_URL = f"{BASE_URL}/Search/Index"
OUTPUT_DIR = "judikatura/Afs/2026"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
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
    print("--- Startuji stahovač NSS (Afs 2026) ---")
    
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        # 1. Krok: Navštívit hlavní stránku pro získání cookies a session
        print("Navštěvuji hlavní stránku pro inicializaci session...")
        init_res = client.get(BASE_URL)
        print(f"Stavový kód hlavní stránky: {init_res.status_code}")
        
        if init_res.status_code != 200:
            print(f"Server NSS odmítl spojení s kódem {init_res.status_code}.")
            return

        page = 1
        total_saved = 0
        
        while True:
            # Parametry hledání: Rejstřík Afs, Rok 2026
            params = {
                "Rejstrik": "Afs",
                "Rok": "2026",
                "Page": str(page)
            }
            
            print(f"Hledám na straně {page}...")
            res = client.get(SEARCH_URL, params=params)
            
            if res.status_code != 200:
                print(f"Chyba při vyhledávání (HTTP {res.status_code})")
                # Zkusíme záložní metodu přes POST
                res = client.post(SEARCH_URL, data=params)
                if res.status_code != 200:
                    print(f"POST pokus také selhal (HTTP {res.status_code}). Končím.")
                    break

            soup = BeautifulSoup(res.text, "html.parser")
            
            # Hledání všech odkazů na originální texty rozhodnutí
            links = soup.find_all("a", href=re.compile(r"/DokumentOriginal/Text/\d+"))
            print(f"Nalezeno {len(links)} odkazů na rozhodnutí na straně {page}.")
            
            if not links:
                if page == 1:
                    print("Na první stránce nebyly nalezeny žádné odkazy. Zkontrolujte, zda pro rok 2026 již existují zveřejněná rozhodnutí Afs, nebo zda server nezměnil HTML strukturu.")
                break

            for link in links:
                doc_rel_url = link["href"]
                doc_url = f"{BASE_URL}{doc_rel_url}" if doc_rel_url.startswith("/") else f"{BASE_URL}/{doc_rel_url}"
                
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
                            total_saved += 1
                        else:
                            print(f"Už máme: {filename}")
                    time.sleep(1.0)
                except Exception as err:
                    print(f"Chyba při stahování {doc_url}: {err}")
                    continue

            page += 1
            if page > 20:  # Bezpečnostní pojistka proti nekonečné smyčce
                break

        print(f"--- Hotovo. Nově staženo {total_saved} souborů. ---")

if __name__ == "__main__":
    run()
