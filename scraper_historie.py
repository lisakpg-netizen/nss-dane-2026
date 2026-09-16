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
    for enc in ["utf-8", "windows-1250", "iso-8859-2"]:
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

def inspect_js_handler(client: httpx.Client, soup: BeautifulSoup):
    """Najde v externích skriptech přesný kód volající MyResTRowsCont a vypíše ho."""
    print("--- Analýza volání /Home/MyResTRowsCont z JavaScriptu ---")
    for s in soup.find_all("script", src=True):
        src_url = urljoin(BASE_URL, s["src"])
        try:
            js_res = client.get(src_url)
            if "MyResTRowsCont" in js_res.text:
                idx = js_res.text.find("MyResTRowsCont")
                snippet = js_res.text[max(0, idx - 200): min(len(js_res.text), idx + 250)]
                print(f"Zdroj: {s['src']}")
                print(f"Kód obsluhy:\n{snippet}\n")
                return snippet
        except Exception:
            continue
    return ""

def run():
    print(f"=== Zahajuji kompletní sběr rozhodnutí pro rok {TARGET_YEAR} ===")
    with httpx.Client(headers=HEADERS, timeout=40.0, follow_redirects=True) as client:
        init_res = client.get(BASE_URL)
        soup = BeautifulSoup(decode_html_safely(init_res), "html.parser")
        form = soup.find("form")
        if not form:
            print("Formulář nebyl nalezen.")
            return

        inspect_js_handler(client, soup)

        form_data = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
        for sel in form.find_all("select"):
            if sel.get("name"):
                opt = sel.find("option", selected=True)
                form_data[sel["name"]] = opt.get("value", "") if opt else ""

        for k in list(form_data.keys()):
            if "vyhledavaciSekce[0].vyhledavaciPodminka[1]" in k:
                if k.endswith(".Visible"): form_data[k] = "True"
                elif k.endswith(".HodnotaText"): form_data[k] = "Afs"
            if "vyhledavaciSekce[1].vyhledavaciPodminka[0]" in k:
                if k.endswith(".Visible"): form_data[k] = "True"
                elif k.endswith(".HodnotaDatumACasOd"): form_data[k] = f"01.01.{TARGET_YEAR}"
                elif k.endswith(".HodnotaDatumACasDo"): form_data[k] = f"31.12.{TARGET_YEAR}"

        print("1. Odesílám úvodní vyhledávací formulář...")
        res = client.post(BASE_URL, data=form_data)
        current_html = decode_html_safely(res)
        res_soup = BeautifulSoup(current_html, "html.parser")

        # Seznam všech nalezených rozhodnutí: (link_tag, doc_url, doc_id)
        all_collected_docs = []
        seen_doc_ids = set()

        def extract_links_from_soup(s_obj):
            new_found = 0
            for link in s_obj.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/|DokumentOriginal/Podrobnosti/")):
                href = link.get("href", "")
                if "Podrobnosti" in href:
                    href = href.replace("Podrobnosti", "Text")
                doc_id = href.rstrip("/").split("/")[-1]
                if doc_id not in seen_doc_ids:
                    seen_doc_ids.add(doc_id)
                    all_collected_docs.append((link, urljoin(BASE_URL, href), doc_id))
                    new_found += 1
            return new_found

        initial_count = extract_links_from_soup(res_soup)
        print(f"Úvodní dávka: načteno prvních {initial_count} rozhodnutí.")

        # 2. Načítání dalších dávek přes /Home/MyResTRowsCont
        cont_url = f"{BASE_URL}/Home/MyResTRowsCont"
        batch = 1
        headers_ajax = HEADERS.copy()
        headers_ajax["X-Requested-With"] = "XMLHttpRequest"

        while True:
            batch += 1
            current_total = len(seen_doc_ids)
            print(f"Volám dávku č. {batch} přes {cont_url} (aktuálně v seznamu: {current_total} rozhodnutí)...")

            # Zkoušíme POST i GET variantu
            chunk_res = client.post(cont_url, data={"from": current_total, "count": 40}, headers=headers_ajax)
            if chunk_res.status_code != 200 or not chunk_res.text.strip():
                chunk_res = client.get(cont_url, params={"from": current_total, "count": 40}, headers=headers_ajax)

            if chunk_res.status_code != 200:
                print(f"Endpoint vrátil HTTP {chunk_res.status_code}. Další data nejsou k dispozici.")
                break

            chunk_html = decode_html_safely(chunk_res)
            chunk_soup = BeautifulSoup(chunk_html, "html.parser")
            new_in_batch = extract_links_from_soup(chunk_soup)

            print(f"-> Dávka č. {batch} přidala {new_in_batch} nových rozhodnutí.")

            if new_in_batch == 0:
                print("Dosažen konec seznamu (žádné další záznamy).")
                break

            time.sleep(0.5)

        print(f"\n=== Celkem nalezeno {len(all_collected_docs)} rozhodnutí pro rok {TARGET_YEAR} ===")

        # 3. Stažení samotných souborů do Markdownu
        saved = 0
        for i, (link_tag, doc_url, doc_id) in enumerate(all_collected_docs, 1):
            try:
                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    md_text, spzn, datum = extract_content(decode_html_safely(doc_res), link_tag, doc_url, doc_id)
                    filename = sanitize_filename(spzn, doc_id)
                    filepath = os.path.join(OUTPUT_DIR, filename)

                    if not os.path.exists(filepath):
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(md_text)
                        saved += 1
                        if saved % 25 == 0 or saved == 1:
                            print(f"[{i}/{len(all_collected_docs)}] Uloženo: {filename} ({datum})")
                    else:
                        pass
                time.sleep(0.4)
            except Exception as e:
                print(f"Chyba u {doc_url}: {e}")

        print(f"\n=== Hotovo! Úspěšně archivováno {saved} nových souborů pro rok {TARGET_YEAR}. ===")

if __name__ == "__main__":
    run()
