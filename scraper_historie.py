import sys
import os
import re
import time
import calendar
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md

TARGET_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
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

def generate_date_slices(year: int):
    """Rozdělí rok na 36 desetidenních úseků, aby výsledky nepřekročily limit 40 na stránku."""
    slices = []
    for month in range(1, 13):
        last_day = calendar.monthrange(year, month)[1]
        slices.append((f"01.{month:02d}.{year}", f"10.{month:02d}.{year}"))
        slices.append((f"11.{month:02d}.{year}", f"20.{month:02d}.{year}"))
        slices.append((f"21.{month:02d}.{year}", f"{last_day:02d}.{month:02d}.{year}"))
    return slices

def run():
    print(f"=== Zahajuji archivaci daňové judikatury (Afs) pro rok {TARGET_YEAR} ===")
    
    with httpx.Client(headers=HEADERS, timeout=40.0, follow_redirects=True) as client:
        init_res = client.get(BASE_URL)
        soup = BeautifulSoup(decode_html_safely(init_res), "html.parser")
        form = soup.find("form")
        if not form:
            print("Chyba: Formulář NSS nebyl nalezen.")
            return

        base_form = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
        for sel in form.find_all("select"):
            if sel.get("name"):
                opt = sel.find("option", selected=True)
                base_form[sel["name"]] = opt.get("value", "") if opt else ""

        # Aktivace filtru pro rejstřík Afs
        for k in list(base_form.keys()):
            if "vyhledavaciSekce[0].vyhledavaciPodminka[1]" in k:
                if k.endswith(".Visible"): base_form[k] = "True"
                elif k.endswith(".HodnotaText"): base_form[k] = "Afs"

        all_collected_docs = []
        seen_doc_ids = set()

        date_slices = generate_date_slices(TARGET_YEAR)
        print(f"1. FÁZE: Procházím rok {TARGET_YEAR} ve {len(date_slices)} časových úsecích...")

        for idx, (d_from, d_to) in enumerate(date_slices, 1):
            current_form = base_form.copy()
            for k in list(current_form.keys()):
                if "vyhledavaciSekce[1].vyhledavaciPodminka[0]" in k:
                    if k.endswith(".Visible"): current_form[k] = "True"
                    elif k.endswith(".HodnotaDatumACasOd"): current_form[k] = d_from
                    elif k.endswith(".HodnotaDatumACasDo"): current_form[k] = d_to

            try:
                res = client.post(BASE_URL, data=current_form)
                res_soup = BeautifulSoup(decode_html_safely(res), "html.parser")
                links = res_soup.find_all("a", href=re.compile(r"DokumentOriginal/Text/|/Text/|DokumentOriginal/Podrobnosti/"))

                slice_new = 0
                for link in links:
                    href = link.get("href", "")
                    if "Podrobnosti" in href:
                        href = href.replace("Podrobnosti", "Text")
                    doc_id = href.rstrip("/").split("/")[-1]
                    if doc_id not in seen_doc_ids:
                        seen_doc_ids.add(doc_id)
                        all_collected_docs.append((link, urljoin(BASE_URL, href), doc_id))
                        slice_new += 1

                print(f"[{idx}/36] {d_from} - {d_to}: nalezeno {slice_new} rozhodnutí (průběžně celkem: {len(seen_doc_ids)})")
                time.sleep(0.3)
            except Exception as e:
                print(f"Chyba u intervalu {d_from}-{d_to}: {e}")

        total_docs = len(all_collected_docs)
        print(f"\n2. FÁZE: Celkem indexováno {total_docs} rozhodnutí. Zahajuji stahování plných textů...")

        saved = 0
        skipped = 0

        for i, (link_tag, doc_url, doc_id) in enumerate(all_collected_docs, 1):
            try:
                # Nejprve zjistíme spzn z odkazu/řádku pro kontrolu existence souboru před stahováním
                row = link_tag.find_parent("tr") if link_tag else None
                spzn_guess = ""
                if row:
                    m = re.search(r'\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b', row.get_text(separator=" ", strip=True), re.I)
                    if m: spzn_guess = m.group(1)

                potential_filename = sanitize_filename(spzn_guess, doc_id)
                potential_filepath = os.path.join(OUTPUT_DIR, potential_filename)

                if os.path.exists(potential_filepath):
                    skipped += 1
                    continue

                doc_res = client.get(doc_url)
                if doc_res.status_code == 200:
                    md_text, spzn, datum = extract_content(decode_html_safely(doc_res), link_tag, doc_url, doc_id)
                    filename = sanitize_filename(spzn, doc_id)
                    filepath = os.path.join(OUTPUT_DIR, filename)

                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(md_text)
                    saved += 1
                    if saved % 20 == 0 or saved == 1:
                        print(f"[{i}/{total_docs}] Uloženo: {filename} ({datum})")
                time.sleep(0.35)
            except Exception as e:
                print(f"Chyba při stahování {doc_url}: {e}")

        print(f"\n=== Hotovo pro rok {TARGET_YEAR}! ===")
        print(f"Nově uloženo: {saved} souborů | Dříve staženo (přeskočeno): {skipped} souborů | Celkem v archivu: {saved + skipped}/{total_docs}")

if __name__ == "__main__":
    run()
