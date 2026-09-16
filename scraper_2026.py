import os
import re
import time
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
    "Referer": "https://vyhledavac.nssoud.cz/",
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

    content_div = soup.find("div", id="divTextRozhodnuti") or soup.find("div", class_="rozhodnuti-fulltext") or soup.find("div", class_="panel-body")
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
    print("--- 1. Získání session a tokenu z vyhledávače NSS ---")
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        init_res = client.get(BASE_URL)
        if init_res.status_code != 200:
            print(f"Chyba připojení: HTTP {init_res.status_code}")
            return

        soup = BeautifulSoup(init_res.text, "html.parser")
        token_input = soup.find("input", {"name": "__RequestVerificationToken"})
        token = token_input.get("value") if token_input else ""
        print(f"Token získán: {'ANO' if token else 'NE'}")

        # Nastavení hledání: Označení věci (spisová značka obsahuje 'Afs' a rok '2026')
        # Alternativní přímý GET dotaz podporovaný rozhraním vyhledávače
        direct_search_url = f"{BASE_URL}/Home/Vysledky"
        
        # Testujeme přímé volání podstránky s výsledky
        print("--- 2. Dotazování na seznam rozhodnutí ---")
        
        # Sestavení parametrů vyhledávání pro Afs v roce 2026
        search_params = {
            "spisovaZnacka": "Afs",
            "rok": "2026",
            "__RequestVerificationToken": token
        }
        
        # Pokus o vyhledání přes standardní rozhraní výsledků
        res = client.post(direct_search_url, data=search_params)
        print(f"Odpověď vyhledávání (Home/Vysledky): HTTP {res.status_code}")
        
        # Pokud toto URL nevrátí výsledky, zkusíme výchozí vyhledávací filtr s aktivovanou podmínkou
        doc_links = []
        if res.status_code == 200:
            res_soup = BeautifulSoup(res.text, "html.parser")
            doc_links = res_soup.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/\d+"))

        # Záložní varianta: Prohledání přímého fulltextového dotazu
        if not doc_links:
            print("Zkouším univerzální vyhledávací dotaz...")
            fallback_url = f"{BASE_URL}/?dotaz=Afs%202026"
            res2 = client.get(fallback_url)
            if res2.status_code == 200:
                res2_soup = BeautifulSoup(res2.text, "html.parser")
                doc_links = res2_soup.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/\d+"))
                print(f"Nalezeno {len(doc_links)} odkazů přes dotaz 'Afs 2026'.")

        if not doc_links:
            print("Automat nenašel žádné přímé odkazy na dokumenty. Vypisuji dostupné formulářové akce z webu:")
            for form in soup.find_all("form"):
                print(f"Formulář action: {form.get('action')}, method: {form.get('method')}")
            return

        print(f"--- 3. Stahování {len(doc_links)} nalezených rozhodnutí ---")
        saved_count = 0
        for link in doc_links:
            href = link.get("href", "")
            doc_url = f"{BASE_URL}{href}" if href.startswith("/") else f"{BASE_URL}/{href}"
            
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
                print(f"Chyba při stahování {doc_url}: {e}")

        print(f"Dokončeno. Celkem nově uloženo: {saved_count}")

if __name__ == "__main__":
    run()
