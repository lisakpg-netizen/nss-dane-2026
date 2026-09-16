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

def decode_html_safely(response: httpx.Response) -> str:
    """Bezpečně dekóduje odpověď serveru NSS i při chybějícím BOM u UTF-16."""
    raw = response.content
    if not raw:
        return ""
    
    # Detekce UTF-16LE bez BOM (každý druhý bajt v ASCII HTML je nulový)
    if len(raw) > 3 and raw[1] == 0 and raw[3] == 0:
        try:
            return raw.decode("utf-16-le", errors="replace")
        except Exception:
            pass

    # Standardní pokusy o dekódování
    for enc in ["utf-8", "windows-1250", "utf-16", "iso-8859-2"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")

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
    return yaml_frontmatter + markdown_text.strip(), spzn, datum

def run():
    print("--- 1. Načítám vyhledávací formulář NSS ---")
    with httpx.Client(headers=HEADERS, timeout=35.0, follow_redirects=True) as client:
        res = client.get(BASE_URL)
        if res.status_code != 200:
            print(f"Chyba spojení s NSS: HTTP {res.status_code}")
            return

        html_home = decode_html_safely(res)
        soup = BeautifulSoup(html_home, "html.parser")
        form = soup.find("form")
        if not form:
            print("Formulář nebyl nalezen.")
            return

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

        for btn in form.find_all(["button", "input"]):
            if btn.get("type") == "submit" and btn.get("name"):
                form_data[btn["name"]] = btn.get("value", "")

        # Aktivace podmínky pro rejstřík Afs
        print("Nastavuji filtr pro daňový senát Afs...")
        for key in list(form_data.keys()):
            if "vyhledavaciSekce[0].vyhledavaciPodminka[1]" in key:
                if key.endswith(".Visible"):
                    form_data[key] = "True"
                elif key.endswith(".HodnotaText"):
                    form_data[key] = "Afs"

        print("--- 2. Odesílám vyhledávání ---")
        post_res = client.post(BASE_URL, data=form_data)
        print(f"Odpověď vyhledávače: HTTP {post_res.status_code}")

        html_search = decode_html_safely(post_res)
        res_soup = BeautifulSoup(html_search, "html.parser")

        links = res_soup.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/|DokumentOriginal/Podrobnosti/"))
        print(f"Celkem nalezeno {len(links)} odkazů na rozhodnutí.")

        if not links:
            return

        print("--- 3. Zpracovávám rozhodnutí pro rok 2026 ---")
        saved = 0
        for link in links:
            href = link.get("href", "")
            if "Podrobnosti" in href:
                href = href.replace("Podrobnosti", "Text")

            doc_url = urljoin(BASE_URL, href)

            try:
                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    html_doc = decode_html_safely(doc_res)
                    md_text, spzn, datum = extract_content(html_doc, doc_url)

                    # Zahrnujeme rozhodnutí týkající se roku 2026 (vydaná v 2026 nebo se značkou z 2026)
                    is_2026 = "2026" in datum or "2026" in spzn
                    if not is_2026:
                        print(f"Přeskakuji starší rozhodnutí: {spzn} (datum: {datum})")
                        continue

                    filename = sanitize_filename(spzn)
                    filepath = os.path.join(OUTPUT_DIR, filename)

                    if not os.path.exists(filepath):
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(md_text)
                        print(f"✅ Uloženo: {filename} ({datum})")
                        saved += 1
                    else:
                        print(f"Už existuje: {filename}")
                time.sleep(0.8)
            except Exception as e:
                print(f"Chyba při stahování {doc_url}: {e}")

        print(f"--- Hotovo! Úspěšně uloženo {saved} nových rozhodnutí pro rok 2026. ---")

if __name__ == "__main__":
    run()
