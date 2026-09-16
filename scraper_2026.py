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
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
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
    print("--- 1. Načítám vyhledávací formulář NSS ---")
    with httpx.Client(headers=HEADERS, timeout=35.0, follow_redirects=True) as client:
        res = client.get(BASE_URL)
        if res.status_code != 200:
            print(f"Chyba spojení s NSS: HTTP {res.status_code}")
            return

        soup = BeautifulSoup(res.text, "html.parser")
        form = soup.find("form")
        if not form:
            print("Formulář nebyl na stránce nalezen.")
            return

        # Posbíráme všechna stávající pole a tokeny
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

        # Přidáme i tlačítka typu submit
        for btn in form.find_all(["button", "input"]):
            if btn.get("type") == "submit" and btn.get("name"):
                form_data[btn["name"]] = btn.get("value", "")

        # 2. Nastavíme vyhledávací podmínku pro spisovou značku Afs
        print("Nastavuji filtr pro daňový senát Afs...")
        for key in list(form_data.keys()):
            # Aktivace pole "Označení věci v celku" (sekce 0, podmínka 1)
            if "vyhledavaciSekce[0].vyhledavaciPodminka[1]" in key:
                if key.endswith(".Visible"):
                    form_data[key] = "True"
                elif key.endswith(".HodnotaText"):
                    form_data[key] = "Afs"

        print("--- 2. Odesílám vyhledávání ---")
        post_res = client.post(BASE_URL, data=form_data)
        print(f"Odpověď vyhledávače: HTTP {post_res.status_code}")

        res_soup = BeautifulSoup(post_res.text, "html.parser")

        # Hledáme všechny odkazy na detail nebo text rozhodnutí
        links = res_soup.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/|DokumentOriginal/Podrobnosti/"))
        print(f"Celkem nalezeno {len(links)} odkazů na rozhodnutí.")

        if not links:
            # Kontrolní výpis v případě, že se struktura ještě liší
            tables = res_soup.find_all("table")
            print(f"Počet nalezených tabulek s výsledky: {len(tables)}")
            text_peek = res_soup.get_text(separator=" ", strip=True)[:300]
            print(f"Ukázka textu stránky: {text_peek}")
            return

        # 3. Stažení a uložení rozhodnutí pro rok 2026
        print("--- 3. Zpracovávám rozhodnutí pro rok 2026 ---")
        saved = 0
        for link in links:
            href = link.get("href", "")
            # Pokud odkaz vede na podrobnosti, převedeme ho na plný text
            if "Podrobnosti" in href:
                href = href.replace("Podrobnosti", "Text")

            doc_url = urljoin(BASE_URL, href)
            link_text = link.get_text(strip=True)

            try:
                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    md_text, spzn = extract_content(doc_res.text, doc_url)

                    # Filtrujeme pouze ročník 2026 (buď ve spisové značce, nebo v textu odkazu)
                    if "2026" not in spzn and "2026" not in link_text and "2026" not in md_text[:500]:
                        print(f"Přeskakuji starší rozhodnutí: {spzn}")
                        continue

                    filename = sanitize_filename(spzn)
                    filepath = os.path.join(OUTPUT_DIR, filename)

                    if not os.path.exists(filepath):
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(md_text)
                        print(f"Uloženo: {filename}")
                        saved += 1
                    else:
                        print(f"Již existuje: {filename}")
                time.sleep(1.0)
            except Exception as e:
                print(f"Chyba při stahování {doc_url}: {e}")

        print(f"--- Hotovo! Úspěšně uloženo {saved} nových rozhodnutí pro rok 2026. ---")

if __name__ == "__main__":
    run()
