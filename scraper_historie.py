import re
from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://vyhledavac.nssoud.cz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9",
    "Referer": f"{BASE_URL}/",
}

def decode_html_safely(res: httpx.Response) -> str:
    raw = res.content
    if len(raw) > 3 and raw[1] == 0 and raw[3] == 0:
        return raw.decode("utf-16-le", errors="replace")
    for enc in ["utf-8", "windows-1250", "iso-8859-2"]:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")

def run():
    print("=== Prohledávám JavaScripty vyhledávače NSS pro odhalení infinite scrollu ===")
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        res = client.get(BASE_URL)
        html = decode_html_safely(res)
        soup = BeautifulSoup(html, "html.parser")

        # 1. Hledání v inline skriptech přímo v HTML
        scripts = [s.string for s in soup.find_all("script") if s.string]
        
        # 2. Hledání externích .js souborů
        for s in soup.find_all("script", src=True):
            src_url = urljoin(BASE_URL, s["src"])
            try:
                js_res = client.get(src_url)
                if js_res.status_code == 200:
                    scripts.append(js_res.text)
            except Exception:
                continue

        print(f"Celkem staženo a analyzováno {len(scripts)} JavaScriptů.")

        # Hledáme zmínky o AJAXu, scrollování a hlášce ze spinneru
        keywords = ["množství dat", "Pracuji", "ajax", "fetch", "scroll", "Dalsi", "GetNext"]
        found = False

        for i, js in enumerate(scripts):
            if any(kw.lower() in js.lower() for kw in ["množství dat", "pracuji", "dalsi"]):
                print(f"\n--- Nalezen relevantní kód v JS souboru č. {i+1} ---")
                found = True
                for line in js.splitlines():
                    if any(w in line for w in ["url", "POST", "GET", "ajax", "fetch", "množství dat", "Pracuji", "table", "dalsi"]):
                        print(line.strip()[:180])

        if not found:
            print("Specifický text nebyl nalezen, vypisuji všechny nalezené URL cesty z JavaScriptů:")
            matches = set(re.findall(r'["\'](/Home/[^"\']+|/[A-Z][a-zA-Z0-9_/]+)["\']', " ".join(scripts)))
            for m in sorted(matches):
                print(f"Nalezená cesta: {m}")

if __name__ == "__main__":
    run()
