import sys
import os
import re
import time
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

TARGET_YEAR = sys.argv[1] if len(sys.argv) > 1 else "2025"
BASE_URL = "https://vyhledavac.nssoud.cz"
OUTPUT_DIR = f"judikatura/Afs/{TARGET_YEAR}"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}

def sanitize_filename(spzn: str, doc_id: str) -> str:
    clean = re.sub(r"[^\w\s-]", "_", spzn) if spzn and spzn != "Neznama_znacka" else f"Afs_{doc_id}"
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

def extract_content(html_doc: str, link_tag, doc_url: str, doc_id: str):
    soup = BeautifulSoup(html_doc, "html.parser")
    full_text = soup.get_text(separator=" ", strip=True)

    spzn, datum, ecli, forma = "", "", "", "Rozsudek"
    row = link_tag.find_parent("tr") if link_tag else None
    if row:
        rt = row.get_text(separator=" ", strip=True)
        m_spzn = re.search(r'\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b', rt, re.I)
        if m_spzn: spzn = m_spzn.group(1)
        m_datum = re.search(r'\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b', rt)
        if m_datum: datum = m_datum.group(1)
        m_ecli = re.search(r'\b(ECLI:CZ:NSS:\d{4}:[^\s]+)\b', rt)
        if m_ecli: ecli = m_ecli.group(1)
        if "usnesení" in rt.lower(): forma = "Usnesení"

    if not spzn:
        m_spzn = re.search(r'\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b', full_text[:2000], re.I)
        spzn = m_spzn.group(1) if m_spzn else f"Afs_doc_{doc_id}"

    if not datum:
        m_datum = re.search(r'ze\s+dne\s+(\d{1,2}\.\s*(?:\d{1,2}\.|[a-záčďéěíňóřšťúůýž]+)\s*\d{4})', full_text[:2500], re.I)
        if not m_datum: m_datum = re.search(r'\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b', full_text[:2500])
        if m_datum: datum = m_datum.group(1)

    content_div = soup.find("div", id="divTextRozhodnuti") or soup.find("div", class_="rozhodnuti-fulltext") or soup.body
    md_body = md(str(content_div), heading_style="ATX", strip=['script', 'style', 'nav', 'footer'])

    yaml_header = f"""---
soud: Nejvyšší správní soud
rejstrik: Afs
spisova_znacka: "{spzn.strip()}"
ecli: "{ecli.strip()}"
datum_rozhodnuti: "{datum.strip()}"
forma: "{forma}"
rok_archivace: {TARGET_YEAR}
zdroj_url: "{doc_url}"
---

"""
    return yaml_header + md_body.strip(), spzn, datum

def run():
    print(f"=== Zahajuji kompletní stahování Afs pro rok {TARGET_YEAR} (včetně všech stran) ===")
    with httpx.Client(headers=HEADERS, timeout=40.0, follow_redirects=True) as client:
        # 1. Získání základních polí formuláře
        init_res = client.get(BASE_URL)
        soup = BeautifulSoup(decode_html_safely(init_res), "html.parser")
        form = soup.find("form")
        if not form:
            print("Formulář nebyl nalezen.")
            return

        base_form = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
        for sel in form.find_all("select"):
            if sel.get("name"):
                opt = sel.find("option", selected=True)
                base_form[sel["name"]] = opt.get("value", "") if opt else ""

        # Nastavení filtrů pro daný rok a rejstřík
        for k in list(base_form.keys()):
            if "vyhledavaciSekce[0].vyhledavaciPodminka[1]" in k:
                if k.endswith(".Visible"): base_form[k] = "True"
                elif k.endswith(".HodnotaText"): base_form[k] = "Afs"
            if "vyhledavaciSekce[1].vyhledavaciPodminka[0]" in k:
                if k.endswith(".Visible"): base_form[k] = "True"
                elif k.endswith(".HodnotaDatumACasOd"): base_form[k] = f"01.01.{TARGET_YEAR}"
                elif k.endswith(".HodnotaDatumACasDo"): base_form[k] = f"31.12.{TARGET_YEAR}"

        page = 1
        total_saved = 0
        seen_doc_ids = set()

        while True:
            print(f"\n--- Načítám stránku {page} ---")
            current_form = base_form.copy()
            current_form["Strana"] = str(page)
            current_form["Page"] = str(page)

            # Odeslání dotazu pro konkrétní stranu
            paged_url = f"{BASE_URL}/Home/Index?page={page}"
            res = client.post(paged_url, data=current_form)
            res_soup = BeautifulSoup(decode_html_safely(res), "html.parser")

            links = res_soup.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/|DokumentOriginal/Podrobnosti/"))
            
            # Filtrujeme pouze unikátní ID z aktuální stránky
            page_docs = []
            for link in links:
                href = link.get("href", "")
                if "Podrobnosti" in href: href = href.replace("Podrobnosti", "Text")
                doc_id = href.rstrip("/").split("/")[-1]
                if doc_id not in seen_doc_ids:
                    seen_doc_ids.add(doc_id)
                    page_docs.append((link, urljoin(BASE_URL, href), doc_id))

            if not page_docs:
                print("Na této stránce už nejsou žádná nová rozhodnutí. Konec ročníku.")
                break

            print(f"Nalezeno {len(page_docs)} nových rozhodnutí na straně {page}.")

            for link_tag, doc_url, doc_id in page_docs:
                try:
                    doc_res = client.get(doc_url)
                    if doc_res.status_code == 200:
                        md_text, spzn, datum = extract_content(decode_html_safely(doc_res), link_tag, doc_url, doc_id)
                        filename = sanitize_filename(spzn, doc_id)
                        filepath = os.path.join(OUTPUT_DIR, filename)

                        if not os.path.exists(filepath):
                            with open(filepath, "w", encoding="utf-8") as f:
                                f.write(md_text)
                            total_saved += 1
                            print(f"[{total_saved}] Uloženo: {filename} ({datum})")
                        else:
                            print(f"Už existuje: {filename}")
                    time.sleep(0.6)
                except Exception as e:
                    print(f"Chyba {doc_url}: {e}")

            page += 1
            if page > 30:  # Bezpečnostní limit proti zacyklení
                break

        print(f"\n=== Hotovo! Celkem uloženo {total_saved} rozhodnutí pro rok {TARGET_YEAR}. ===")

if __name__ == "__main__":
    run()
