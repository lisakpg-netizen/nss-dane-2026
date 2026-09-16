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

def sanitize_filename(spzn: str, doc_id: str) -> str:
    if not spzn or spzn == "Neznama_znacka":
        return f"NSS_Afs_{doc_id}.md"
    clean = re.sub(r"[^\w\s-]", "_", spzn)
    clean = re.sub(r"\s+", "_", clean.strip())
    return f"NSS_{clean}.md"

def decode_html_safely(response: httpx.Response) -> str:
    raw = response.content
    if not raw:
        return ""
    if len(raw) > 3 and raw[1] == 0 and raw[3] == 0:
        try:
            return raw.decode("utf-16-le", errors="replace")
        except Exception:
            pass
    for enc in ["utf-8", "windows-1250", "utf-16", "iso-8859-2"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")

def extract_metadata_and_markdown(html_doc: str, link_tag, doc_url: str, doc_id: str):
    soup = BeautifulSoup(html_doc, "html.parser")
    full_text = soup.get_text(separator=" ", strip=True)

    spzn = ""
    datum = ""
    ecli = ""
    forma = "Rozsudek"

    # 1. Pokus: vytáhnout metadata z řádku vyhledávací tabulky
    parent_row = link_tag.find_parent("tr") if link_tag else None
    if parent_row:
        row_text = parent_row.get_text(separator=" ", strip=True)
        m_spzn = re.search(r'\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b', row_text, re.I)
        if m_spzn:
            spzn = m_spzn.group(1)

        m_datum = re.search(r'\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b', row_text)
        if m_datum:
            datum = m_datum.group(1)

        m_ecli = re.search(r'\b(ECLI:CZ:NSS:\d{4}:[^\s]+)\b', row_text)
        if m_ecli:
            ecli = m_ecli.group(1)

        if "usnesení" in row_text.lower():
            forma = "Usnesení"

    # 2. Pokus: dohledat chybějící údaje v samotném textu rozhodnutí
    if not spzn:
        title_text = soup.title.get_text(strip=True) if soup.title else ""
        m_spzn = re.search(r'\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b', title_text, re.I)
        if not m_spzn:
            m_spzn = re.search(r'\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b', full_text[:2000], re.I)
        if m_spzn:
            spzn = m_spzn.group(1)
        else:
            spzn = f"Afs_doc_{doc_id}"

    if not datum:
        m_datum = re.search(r'ze\s+dne\s+(\d{1,2}\.\s*(?:\d{1,2}\.|[a-záčďéěíňóřšťúůýž]+)\s*\d{4})', full_text[:2500], re.I)
        if not m_datum:
            m_datum = re.search(r'\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b', full_text[:2500])
        if m_datum:
            datum = m_datum.group(1)

    if not ecli:
        m_ecli = re.search(r'\b(ECLI:CZ:NSS:\d{4}:[^\s<"]+)\b', full_text)
        if m_ecli:
            ecli = m_ecli.group(1)

    spzn = re.sub(r'\s+', ' ', spzn).strip()

    # Extrakce těla rozhodnutí a převod do Markdownu
    content_div = (
        soup.find("div", id="divTextRozhodnuti") or 
        soup.find("div", class_="rozhodnuti-fulltext") or 
        soup.find("div", class_="panel-body") or
        soup.body
    )
    markdown_body = md(str(content_div), heading_style="ATX", strip=['script', 'style', 'nav', 'footer'])

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
    return yaml_frontmatter + markdown_body.strip(), spzn, datum, full_text[:2000]

def run():
    print("--- 1. Načítám vyhledávač NSS ---")
    with httpx.Client(headers=HEADERS, timeout=35.0, follow_redirects=True) as client:
        res = client.get(BASE_URL)
        if res.status_code != 200:
            print(f"Chyba spojení: HTTP {res.status_code}")
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
            doc_id = href.rstrip("/").split("/")[-1]

            try:
                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    html_doc = decode_html_safely(doc_res)
                    md_text, spzn, datum, head_preview = extract_metadata_and_markdown(html_doc, link, doc_url, doc_id)

                    # Rozhodnutí patří k roku 2026, pokud má 2026 v datu, ve spisové značce nebo v záhlaví
                    is_2026 = ("2026" in datum) or ("2026" in spzn) or ("2026" in head_preview)
                    
                    if not is_2026:
                        print(f"Přeskakuji starší rozhodnutí: {spzn} (datum: '{datum}')")
                        continue

                    filename = sanitize_filename(spzn, doc_id)
                    filepath = os.path.join(OUTPUT_DIR, filename)

                    if not os.path.exists(filepath):
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(md_text)
                        print(f"✅ Uloženo: {filename} (Datum: {datum or 'neuvedeno'})")
                        saved += 1
                    else:
                        print(f"Už existuje: {filename}")
                time.sleep(0.8)
            except Exception as e:
                print(f"Chyba při stahování {doc_url}: {e}")

        print(f"--- Hotovo! Úspěšně uloženo {saved} nových rozhodnutí pro rok 2026. ---")

if __name__ == "__main__":
    run()
