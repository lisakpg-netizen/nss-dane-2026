import email.utils
import json
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import time
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
import httpx
from markdownify import markdownify as md

# 1. KONFIGURACE PŘÍSTUPŮ A CEST
# ------------------------------------------------------------------------------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()
EMAIL_PASS = os.environ.get("EMAIL_PASS", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip()

BASE_URL = "https://vyhledavac.nssoud.cz"
OUTPUT_DIR = "judikatura/Afs/2026"
DB_FILE = "archiv_nss.json"
os.makedirs(OUTPUT_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}


def nacti_archiv():
  if os.path.exists(DB_FILE):
    with open(DB_FILE, "r", encoding="utf-8") as f:
      return json.load(f)
  return []


def uloz_archiv(archiv):
  with open(DB_FILE, "w", encoding="utf-8") as f:
    json.dump(sorted(list(set(archiv))), f, ensure_ascii=False, indent=2)


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


# 2. EXTRAKCE METADAT A TEXTU ROZHODNUTÍ
# ------------------------------------------------------------------------------
def extract_metadata_and_markdown(
    html_doc: str, link_tag, doc_url: str, doc_id: str
):
  soup = BeautifulSoup(html_doc, "html.parser")
  full_text = soup.get_text(separator=" ", strip=True)

  spzn = ""
  datum = ""
  ecli = ""
  forma = "Rozsudek"

  # 1. Pokus: z řádku vyhledávací tabulky
  parent_row = link_tag.find_parent("tr") if link_tag else None
  if parent_row:
    row_text = parent_row.get_text(separator=" ", strip=True)
    m_spzn = re.search(
        r"\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b", row_text, re.I
    )
    if m_spzn:
      spzn = m_spzn.group(1)

    m_datum = re.search(r"\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b", row_text)
    if m_datum:
      datum = m_datum.group(1)

    m_ecli = re.search(r"\b(ECLI:CZ:NSS:\d{4}:[^\s]+)\b", row_text)
    if m_ecli:
      ecli = m_ecli.group(1)

    if "usnesení" in row_text.lower():
      forma = "Usnesení"

  # 2. Pokus: dohledání přímo v textu rozhodnutí
  if not spzn:
    title_text = soup.title.get_text(strip=True) if soup.title else ""
    m_spzn = re.search(
        r"\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b", title_text, re.I
    )
    if not m_spzn:
      m_spzn = re.search(
        r"\b(\d{1,2}\s*Afs\s*\d{1,4}/\d{4}(?:\s*-\s*\d+)?)\b",
        full_text[:2000],
        re.I,
    )
    if m_spzn:
      spzn = m_spzn.group(1)
    else:
      spzn = f"Afs_doc_{doc_id}"

  if not datum:
    m_datum = re.search(
        r"ze\s+dne\s+(\d{1,2}\.\s*(?:\d{1,2}\.|[a-záčďéěíňóřšťúůýž]+)\s*\d{4})",
        full_text[:2500],
        re.I,
    )
    if not m_datum:
      m_datum = re.search(
          r"\b(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})\b", full_text[:2500]
      )
    if m_datum:
      datum = m_datum.group(1)

  if not ecli:
    m_ecli = re.search(r"\b(ECLI:CZ:NSS:\d{4}:[^\s<"]+)\b", full_text)
    if m_ecli:
      ecli = m_ecli.group(1)

  spzn = re.sub(r"\s+", " ", spzn).strip()

  content_div = (
      soup.find("div", id="divTextRozhodnuti")
      or soup.find("div", class_="rozhodnuti-fulltext")
      or soup.find("div", class_="panel-body")
      or soup.body
  )
  markdown_body = md(
      str(content_div),
      heading_style="ATX",
      strip=["script", "style", "nav", "footer"],
  )

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
  return (
      yaml_frontmatter + markdown_body.strip(),
      spzn,
      datum,
      ecli,
      forma,
      full_text,
  )


# 3. AI SHRNUTÍ ROZSUDKU (GEMINI)
# ------------------------------------------------------------------------------
def vytvor_ai_shrnuti_nss(spzn, datum, forma, full_text, client):
  """Vygeneruje stručný a tvrdý právní rozbor pro daňové poradce."""
  fallback = {
      "spor": (
          f"Přezkum rozhodnutí krajského soudu a daňových orgánů ve věci {spzn}."
      ),
      "vyrok": (
          "Podrobné odůvodnění a výrok jsou uvedeny v plném znění rozhodnutí."
      ),
  }

  if not client:
    return fallback

  # Pro optimální kontext předáme začátek (výrok + reálie) a závěr odůvodnění
  text_pro_ai = (
      full_text[:10000]
      + "\n\n[...]\n\n"
      + (full_text[-8000:] if len(full_text) > 10000 else "")
  )

  prompt = f"""
Jsi špičkový advokát a daňový poradce specializovaný na daňovou judikaturu Nejvyššího správního soudu ČR.
Z přiloženého rozhodnutí NSS připrav stručný, úderný a vysoce věcný výtah pro daňový newsletter kolegům seniorním poradcům a finančním ředitelům.

SPISOVÁ ZNAČKA: {spzn}
FORMA: {forma} | DATUM: {datum}

TEXT ROZHODNUTÍ:
{text_pro_ai}

STRIKTNÍ POŽADAVKY:
1. "spor":
   - Max. 1 až 2 věty.
   - Popiš přesně jádro daňového sporu: O jakou daň šlo a co správce daně / OFŘ daňovému subjektu vytýkal či doměřil (např. neuznání nákladů, zneužití práva u DPH, neprokázání osvobození, procesní vada).
   - ZÁKAZ vaty: Nikdy nezačínej frázemi jako "V této věci se jednalo o". Jdi přímo k meritu.

2. "vyrok":
   - Max. 2 až 3 věty.
   - ZAČNI PŘÍMO VÝSLEDNÝM VERDIKTEM (např. 'Kasační stížnost daňového subjektu ZAMÍTNUTA.', 'Rozsudek krajského soudu ZRUŠEN pro nepřezkoumatelnost.', 'Kasační stížnosti OFŘ VYHOVĚNO.').
   - Uveď KLÍČOVÝ PRÁVNÍ DŮVOD / PRÁVNÍ VĚTU: Proč takto NSS rozhodl, jaké pravidlo formuloval a o jaké klíčové ustanovení zákona (ZDP, ZDPH, daňový řád) se opřel.

Vrať POUZE validní formát JSON:
{{"spor": "...", "vyrok": "..."}}
"""

  for model_name in ["gemini-2.5-flash", "gemini-2.0-flash"]:
    try:
      res = client.models.generate_content(
          model=model_name,
          contents=prompt,
          config=types.GenerateContentConfig(
              temperature=0.1, response_mime_type="application/json"
          ),
      )
      raw = res.text.strip()
      raw = re.sub(r"^```json\s*", "", raw)
      raw = re.sub(r"\s*```$", "", raw)
      data = json.loads(raw)
      if "spor" in data and "vyrok" in data:
        print(f"  ✓ Judikát {spzn} úspěšně analyzován modelem Gemini.")
        return {"spor": data["spor"].strip(), "vyrok": data["vyrok"].strip()}
    except Exception as e:
      if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
        print(f"  ⏳ Limit kvóty (429) u {spzn}. Čekám 30 s...")
        time.sleep(30)
      else:
        print(f"  ❌ Chyba volání Gemini u {spzn}: {e}")
        break

  return fallback


# 4. ODESLÁNÍ E-MAILOVÉHO DIGESTU
# ------------------------------------------------------------------------------
def odesli_tydenni_email_nss(nove_judikaty):
  if not EMAIL_USER or not EMAIL_PASS or not EMAIL_TO:
    print("❌ Chybí e-mailové přihlašovací údaje v GitHub Secrets.")
    return

  msg = MIMEMultipart("alternative")
  msg["Subject"] = (
      f"⚖️ NSS Judikatura (Afs): {len(nove_judikaty)} nových daňových rozhodnutí"
  )
  msg["From"] = f"NSS Daňový Watcher <{EMAIL_USER}>"
  msg["To"] = EMAIL_TO
  msg["Date"] = email.utils.formatdate(localtime=True)

  html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1e293b; line-height: 1.6; max-width: 760px; margin: 0 auto; padding: 24px 16px; background-color: #ffffff;">
        <h2 style="color: #0f172a; font-size: 22px; font-weight: 700; margin: 0 0 8px 0; border-bottom: 2px solid #0f172a; padding-bottom: 10px;">
            ⚖️ Týdenní přehled: Nová daňová judikatura NSS (senát Afs)
        </h2>
        <p style="color: #475569; font-size: 14px; margin: 0 0 24px 0;">
            Za uplynulý týden bylo v databázi NSS zveřejněno <strong>{len(nove_judikaty)} nových daňových rozhodnutí</strong>.
        </p>
    """

  for item in nove_judikaty:
    spor_html = item["ai_shrnuti"]["spor"].replace("\n", "<br>")
    vyrok_html = item["ai_shrnuti"]["vyrok"].replace("\n", "<br>")

    ecli_tag = (
        f'<span style="background-color: #e2e8f0; color: #334155; padding:'
        f' 2px 6px; border-radius: 4px; font-size: 11.5px; font-family:'
        f' monospace;">{item["ecli"]}</span>'
        if item["ecli"]
        else ""
    )

    html += f"""
        <div style="margin-bottom: 24px; padding: 18px 20px; border: 1px solid #cbd5e1; border-radius: 8px; background-color: #ffffff;">
            <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px;">
                <span style="font-size: 17px; font-weight: 700; color: #0f172a;">
                    {item['forma']}: {item['spzn']}
                </span>
            </div>
            
            <div style="font-size: 12.5px; color: #64748b; margin-bottom: 14px;">
                Datum rozhodnutí: <strong style="color: #1e293b;">{item['datum']}</strong> &nbsp;|&nbsp; 
                Soud: <strong style="color: #0f172a;">NSS (Afs)</strong> &nbsp;|&nbsp; {ecli_tag}
            </div>

            <div style="background-color: #f8fafc; border-left: 4px solid #0284c7; padding: 14px 16px; border-radius: 4px; font-size: 13.5px; color: #334155; line-height: 1.6; margin-bottom: 12px;">
                <div style="margin-bottom: 10px;">
                    <strong style="color: #0369a1; display: block; margin-bottom: 3px;">Podstata daňového sporu:</strong>
                    {spor_html}
                </div>
                <div>
                    <strong style="color: #0369a1; display: block; margin-bottom: 3px;">Výrok a nosné důvody NSS:</strong>
                    {vyrok_html}
                </div>
            </div>

            <div style="font-size: 13px;">
                🔗 <a href="{item['url']}" target="_blank" style="color: #0284c7; text-decoration: none; font-weight: 600;">
                    Zobrazit celé rozhodnutí ve vyhledávači NSS &rarr;
                </a>
            </div>
        </div>
        """

  html += """
        <hr style="border: none; border-top: 1px solid #e2e8f0; margin-top: 36px; margin-bottom: 16px;">
        <p style="font-size: 12px; color: #94a3b8; text-align: center; margin: 0;">
            Rozhodnutí jsou uložena v Markdown formátu v repozitáři nss-dane-2026.
        </p>
    </body>
    </html>
    """

  msg.attach(MIMEText(html, "html", "utf-8"))

  with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(EMAIL_USER, EMAIL_PASS)
    server.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())

  print(f"E-mail úspěšně odeslán na {EMAIL_TO}.")


# 5. HLAVNÍ SPOUŠTĚCÍ SMYČKA
# ------------------------------------------------------------------------------
def main():
  print("--- KONTROLA NASTAVENÍ NSS WATCHER ---")
  if GEMINI_KEY:
    print(f"GEMINI_API_KEY nalezen: Ano (délka: {len(GEMINI_KEY)} znaků)")
    client = genai.Client(api_key=GEMINI_KEY)
  else:
    print("⚠️ GEMINI_API_KEY nenalezen, použije se textový fallback.")
    client = None

  archiv = nacti_archiv()
  print(f"V archivu je dosud evidováno {len(archiv)} rozhodnutí.")

  with httpx.Client(
      headers=HEADERS, timeout=40.0, follow_redirects=True
  ) as http_client:
    print("1. Otevírám vyhledávač NSS...")
    res = http_client.get(BASE_URL)
    if res.status_code != 200:
      print(f"Chyba spojení s vyhledávačem NSS: HTTP {res.status_code}")
      return

    html_home = decode_html_safely(res)
    soup = BeautifulSoup(html_home, "html.parser")
    form = soup.find("form")
    if not form:
      print("Vyhledávací formulář nebyl nalezen.")
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

    # Nastavení filtru pro senát Afs
    for key in list(form_data.keys()):
      if "vyhledavaciSekce[0].vyhledavaciPodminka[1]" in key:
        if key.endswith(".Visible"):
          form_data[key] = "True"
        elif key.endswith(".HodnotaText"):
          form_data[key] = "Afs"

    print("2. Odesílám dotaz na senát Afs...")
    post_res = http_client.post(BASE_URL, data=form_data)
    html_search = decode_html_safely(post_res)
    res_soup = BeautifulSoup(html_search, "html.parser")

    links = res_soup.find_all(
        "a",
        href=re.compile(
            r"DokumentOriginal/Text/|/Text/|DokumentOriginal/Podrobnosti/"
        ),
    )
    print(f"Nalezeno {len(links)} odkazů na rozhodnutí.")

    zpracovane_novinky = []

    for link in links:
      href = link.get("href", "")
      if "Podrobnosti" in href:
        href = href.replace("Podrobnosti", "Text")

      doc_url = urljoin(BASE_URL, href)
      doc_id = href.rstrip("/").split("/")[-1]

      # Rychlá kontrola podle doc_id v archivu
      if doc_id in archiv:
        continue

      try:
        doc_res = http_client.get(doc_url)
        if doc_res.status_code != 200:
          continue

        html_doc = decode_html_safely(doc_res)
        (
            md_text,
            spzn,
            datum,
            ecli,
            forma,
            full_text,
        ) = extract_metadata_and_markdown(html_doc, link, doc_url, doc_id)

        # Kontrola, zda rozhodnutí patří k roku 2026
        is_2026 = (
            ("2026" in datum) or ("2026" in spzn) or ("2026" in full_text[:1500])
        )
        if not is_2026:
          continue

        # Kontrola podle spisové značky
        if spzn in archiv:
          continue

        print(f"\nNové rozhodnutí: {spzn} ({datum})...")

        # 1. Uložení do Markdown souboru v repozitáři
        filename = sanitize_filename(spzn, doc_id)
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
          f.write(md_text)
        print(f"  ✓ Uloženo do {filepath}")

        # 2. Analýza přes Gemini
        ai_shrnuti = vytvor_ai_shrnuti_nss(
            spzn, datum, forma, full_text, client
        )

        polozka = {
            "spzn": spzn,
            "datum": datum,
            "ecli": ecli,
            "forma": forma,
            "url": doc_url,
            "ai_shrnuti": ai_shrnuti,
        }
        zpracovane_novinky.append(polozka)

        # Evidence do archivu
        archiv.append(doc_id)
        archiv.append(spzn)

        time.sleep(2)  # Šetření limitů Google AI
      except Exception as e:
        print(f"Chyba při zpracování {doc_url}: {e}")

    if not zpracovane_novinky:
      print("\nŽádná nová rozhodnutí senátu Afs k odeslání.")
      uloz_archiv(archiv)
      return

    print(
        f"\nOdesílám týdenní e-mail s {len(zpracovane_novinky)} novými"
        " rozsudky..."
    )
    odesli_tydenni_email_nss(zpracovane_novinky)
    uloz_archiv(archiv)
    print("Vše úspěšně dokončeno.")


if __name__ == "__main__":
  main()
