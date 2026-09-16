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
}

def run():
    print("=== KROK 1: Testuji spojení s webem NSS ===")
    
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            home = client.get(BASE_URL)
            print(f"Odpověď hlavní stránky: HTTP {home.status_code}")
            
            if home.status_code != 200:
                print(f"POZOR: Server NSS vrátil chybový kód {home.status_code}. Přístup z GitHubu je pravděpodobně blokován.")
                return

        except Exception as err:
            print(f"Spojení se serverem NSS selhalo: {err}")
            return

        print("\n=== KROK 2: Pokus o vyhledání rozhodnutí pro senát Afs (rok 2026) ===")
        # Vyhledávač NSS podporuje přímé parametry v URL
        search_urls = [
            f"{BASE_URL}/Search/Index?Rejstrik=Afs&Rok=2026",
            f"{BASE_URL}/?Rejstrik=Afs&Rok=2026",
            f"{BASE_URL}/DokumentOriginal/Index?Rejstrik=Afs&Rok=2026"
        ]

        found_links = []
        for url in search_urls:
            print(f"Zkouším URL: {url}")
            try:
                res = client.get(url)
                print(f"-> Stavový kód: {res.status_code}")
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, "html.parser")
                    # Hledáme jakékoliv odkazy vedoucí na text rozhodnutí
                    links = soup.find_all("a", href=re.compile(r"/DokumentOriginal/Text/|/Text/"))
                    if links:
                        print(f"-> ÚSPĚCH: Nalezeno {len(links)} odkazů na rozhodnutí!")
                        found_links = links
                        break
                    else:
                        # Zjistíme, co je na stránce za text
                        title = soup.title.string if soup.title else "Bez titulku"
                        print(f"-> Žádné odkazy nenalezeny. Titulek stránky: '{title.strip()}'")
            except Exception as e:
                print(f"-> Chyba při dotazu na {url}: {e}")

        if not found_links:
            print("\nZÁVĚR DIAGNOSTIKY: Server neodpověděl žádnými odkazy na rozhodnutí.")
            print("To znamená, že buď pro rok 2026 zatím neeviduje záznamy, nebo vyžaduje vyplnění interního stavového formuláře.")
            return

        print(f"\n=== KROK 3: Stahuji {len(found_links)} nalezených rozhodnutí ===")
        for a in found_links[:5]:  # Pro test zkusíme prvních 5
            href = a["href"]
            doc_url = f"{BASE_URL}{href}" if href.startswith("/") else f"{BASE_URL}/{href}"
            print(f"Stahuji: {doc_url}")
            try:
                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    soup_doc = BeautifulSoup(doc_res.text, "html.parser")
                    text_div = soup_doc.find("div", id="divTextRozhodnuti") or soup_doc.body
                    md_text = md(str(text_div), heading_style="ATX", strip=['script', 'style'])
                    
                    doc_id = href.split("/")[-1]
                    file_path = os.path.join(OUTPUT_DIR, f"NSS_Afs_2026_{doc_id}.md")
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(md_text)
                    print(f"Uloženo: {file_path}")
                time.sleep(1.0)
            except Exception as e:
                print(f"Chyba u souboru {doc_url}: {e}")

if __name__ == "__main__":
    run()
