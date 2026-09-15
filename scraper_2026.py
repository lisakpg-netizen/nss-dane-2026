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
    "User-Agent": "Mozilla/5.0 (compatible; TaxResearchBot/1.0)",
    "Content-Type": "application/x-www-form-urlencoded"
}

def sanitize_filename(spzn: str) -> str:
    """Převede spisovou značku (např. 1 Afs 15/2026 - 18) na bezpečný název souboru."""
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
    print("Spouštím kontrolu nových rozhodnutí Afs za rok 2026...")
    with httpx.Client(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        page = 1
        while True:
            payload = {
                "Rejstrik": "Afs",
                "Rok": "2026",
                "Page": str(page)
            }
            
            try:
                res = client.post(SEARCH_URL, data=payload)
                if res.status_code != 200:
                    break
            except Exception as e:
                print(f"Chyba sítě: {e}")
                break

            soup = BeautifulSoup(res.text, "html.parser")
            links = soup.find_all("a", href=re.compile(r"/DokumentOriginal/Text/\d+"))
            
            if not links:
                break  # Došli jsme na konec stránek

            for link in links:
                doc_url = f"{BASE_URL}{link['href']}"
                
                try:
                    doc_res = client.get(doc_url)
                    if doc_res.status_code == 200:
                        md_text, spzn = extract_content(doc_res.text, doc_url)
                        filename = sanitize_filename(spzn)
                        filepath = os.path.join(OUTPUT_DIR, filename)
                        
                        # Pokud soubor již existuje, nestahujeme jej znovu
                        if not os.path.exists(filepath):
                            with open(filepath, "w", encoding="utf-8") as f:
                                f.write(md_text)
                            print(f"Nově uloženo: {filename}")
                        else:
                            print(f"Přeskočeno (již existuje): {filename}")
                    time.sleep(1.0)  # Pauza, abychom nepřetížili server soudu
                except Exception as err:
                    print(f"Chyba při stahování: {err}")
                    continue

            page += 1

if __name__ == "__main__":
    run()
