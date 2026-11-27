import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import types

STORE_DISPLAY_NAME = "nss-database"
SOURCE_REPO = "nss"
DEFAULT_DOC_TYPE = "Judikatura"
DEFAULT_AUTHORITY = "binding"

# Nově nahrávat pouze rozsudky od roku 2015 včetně (dříve nahrané starší rozsudky se NEMAŽOU)
DEFAULT_MIN_YEAR = 2015

ROOT_DIR = Path(__file__).resolve().parent.parent
JUDIKATURA_DIR = ROOT_DIR / "judikatura"
STATE_FILE = ROOT_DIR / "registry" / "rag_index.json"


def get_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Chybí proměnná prostředí GEMINI_API_KEY.")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1beta"),
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"store_name": "", "store_display_name": STORE_DISPLAY_NAME, "files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_or_create_store(client: genai.Client, state: dict) -> str:
    saved_name = (state.get("store_name") or "").strip()
    if saved_name:
        try:
            st = client.file_search_stores.get(name=saved_name)
            if getattr(st, "name", None):
                return st.name
        except Exception:
            pass

    for st in client.file_search_stores.list():
        if getattr(st, "display_name", "") == STORE_DISPLAY_NAME:
            return st.name

    created = client.file_search_stores.create(
        config={"display_name": STORE_DISPLAY_NAME}
    )
    print(f"Vytvořen nový File Search Store: {created.name} ({STORE_DISPLAY_NAME})")
    return created.name


def fetch_existing_remote_docs(
    client: genai.Client, store_name: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Načte dokumenty už nahrané ve Storu, aby se při obnově indexu nenahrávaly znovu."""
    by_rel: dict[str, str] = {}
    by_title: dict[str, str] = {}
    try:
        for doc in client.file_search_stores.documents.list(parent=store_name):
            doc_name = getattr(doc, "name", "") or ""
            if not doc_name:
                continue
            title = getattr(doc, "display_name", "") or ""
            if title and title not in by_title:
                by_title[title] = doc_name
            for item in getattr(doc, "custom_metadata", None) or []:
                if getattr(item, "key", None) == "rel_path":
                    rel_val = getattr(item, "string_value", None) or ""
                    if rel_val:
                        by_rel[rel_val] = doc_name
    except Exception as exc:
        print(f"Varování při čtení existujících dokumentů ze Storu: {exc}")
    return by_rel, by_title


def parse_nss_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, text

    meta: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, val = line.split(":", 1)
        key = key.strip().lower()
        val = val.strip().strip('"').strip("'")
        if key and val:
            meta[key] = val

    body = "\n".join(lines[end_idx + 1 :]).strip()
    return meta, body


def detect_forma(fm: dict[str, str], body: str) -> str:
    """Spolehlivě určí formu rozhodnutí přímo ze záhlaví textu (ROZSUDEK vs. USNESENÍ)."""
    header_sample = re.sub(r"\s+", " ", body[:2000])
    if re.search(
        r"r\s*o\s*z\s*s\s*u\s*d\s*e\s*k\s+j\s*m\s*[eé]\s*n\s*e\s*m",
        header_sample,
        re.IGNORECASE,
    ):
        return "Rozsudek"

    if re.search(
        r"(?:^|\n|#|\*)\s*u\s*s\s*n\s*e\s*s\s*e\s*n\s*[ií]\b",
        body[:1500],
        re.IGNORECASE,
    ):
        return "Usnesení"

    forma_yaml = fm.get("forma", "").strip().lower()
    if "usnesení" in forma_yaml or "usneseni" in forma_yaml:
        return "Usnesení"
    return "Rozsudek"


def resolve_year(path: Path, fm: dict[str, str]) -> int:
    rel_parts = path.relative_to(ROOT_DIR).parts
    if len(rel_parts) >= 4 and rel_parts[2].isdigit():
        return int(rel_parts[2])
    if fm.get("rok_archivace", "").isdigit():
        return int(fm["rok_archivace"])
    m_date = re.search(r"\b(20\d{2})\b", fm.get("datum_rozhodnuti", ""))
    if m_date:
        return int(m_date.group(1))
    m_spzn = re.search(r"/(20\d{2})\b", fm.get("spisova_znacka", ""))
    if m_spzn:
        return int(m_spzn.group(1))
    return 0


def collect_candidate_files() -> list[Path]:
    if not JUDIKATURA_DIR.exists():
        return []
    return sorted(JUDIKATURA_DIR.rglob("*.md"))


def build_metadata(
    path: Path, fm: dict[str, str], resolved_forma: str, year_val: int
) -> tuple[str, list[dict[str, str]]]:
    rel = path.relative_to(ROOT_DIR).as_posix()
    parts = rel.split("/")
    rejstrik_folder = parts[1].lower() if len(parts) >= 3 else "afs"

    spzn = fm.get("spisova_znacka", "").strip() or path.stem.replace("NSS_", "").replace("_", " ")
    ecli = fm.get("ecli", "").strip()
    datum = fm.get("datum_rozhodnuti", "").strip()
    rejstrik = fm.get("rejstrik", rejstrik_folder).strip().lower()
    rok = str(year_val) if year_val > 0 else ""

    slug = re.sub(r"[^a-z0-9]+", "-", spzn.lower()).strip("-") or path.stem.lower()
    display_name = (
        f"NSS {spzn} ({resolved_forma}, {datum})"
        if datum
        else f"NSS {spzn} ({resolved_forma})"
    )

    raw_meta: dict[str, str] = {
        "source_repo": SOURCE_REPO,
        "slug": slug,
        "category": rejstrik,
        "doc_type": DEFAULT_DOC_TYPE,
        "authority": DEFAULT_AUTHORITY,
        "spisova_znacka": spzn,
        "ecli": ecli,
        "forma": resolved_forma,
        "rok": rok,
        "datum_rozhodnuti": datum,
        "rel_path": rel,
    }

    custom_metadata = [
        {"key": k, "string_value": str(v)[:200]}
        for k, v in raw_meta.items()
        if v
    ]
    return display_name[:120], custom_metadata


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def delete_remote_document(client: genai.Client, doc_name: str) -> None:
    if not doc_name:
        return
    try:
        client.file_search_stores.documents.delete(
            name=doc_name,
            config={"force": True},
        )
    except Exception as exc:
        if "404" not in str(exc) and "NOT_FOUND" not in str(exc):
            print(f"  Varování: nepodařilo se smazat {doc_name}: {exc}")


def upload_document(
    client: genai.Client,
    store_name: str,
    path: Path,
    display_name: str,
    custom_metadata: list[dict[str, str]],
) -> str:
    suffix = path.suffix.lower() or ".md"
    with tempfile.NamedTemporaryFile(prefix="rag_nss_", suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        shutil.copyfile(path, tmp_path)
        op = client.file_search_stores.upload_to_file_search_store(
            file=str(tmp_path),
            file_search_store_name=store_name,
            config={
                "display_name": display_name,
                "mime_type": "text/markdown",
                "custom_metadata": custom_metadata,
                "chunking_config": {
                    "white_space_config": {
                        "max_tokens_per_chunk": 500,
                        "max_overlap_tokens": 60,
                    }
                },
            },
        )

        deadline = time.time() + 180
        while not getattr(op, "done", False):
            if time.time() > deadline:
                raise TimeoutError(f"Vypršel limit při indexaci souboru {path.name}")
            time.sleep(2)
            op = client.operations.get(op)

        err = getattr(op, "error", None)
        if err:
            raise RuntimeError(f"Chyba indexace {path.name}: {err}")

        resp = getattr(op, "response", None)
        doc_name = getattr(resp, "document_name", "") or ""
        if not doc_name:
            for doc in client.file_search_stores.documents.list(parent=store_name):
                if getattr(doc, "display_name", "") == display_name:
                    doc_name = getattr(doc, "name", "") or ""
                    break
        return doc_name
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> None:
    force_resync = os.getenv("FORCE_RESYNC", "").lower() in {"1", "true", "yes"}
    max_uploads = int(os.getenv("MAX_UPLOADS", "250") or "250")
    env_year = os.getenv("MIN_YEAR", "").strip()
    min_year = int(env_year) if env_year.isdigit() else DEFAULT_MIN_YEAR

    client = get_client()
    state = load_state()
    store_name = get_or_create_store(client, state)
    state["store_name"] = store_name
    state["store_display_name"] = STORE_DISPLAY_NAME

    # 1. Najdeme VŠECHNY platné rozsudky napříč všemi ročníky (abychom starší už nahrané nesmazali)
    all_rozsudky: list[tuple[str, Path, dict[str, str], str, int]] = []
    excluded_usneseni = 0

    for path in collect_candidate_files():
        raw_text = path.read_text("utf-8", errors="replace")
        fm, body = parse_nss_frontmatter(raw_text)
        if len(body.strip()) < 100:
            excluded_usneseni += 1
            continue
        forma = detect_forma(fm, body)
        if forma != "Rozsudek":
            excluded_usneseni += 1
            continue
        rel = path.relative_to(ROOT_DIR).as_posix()
        year_val = resolve_year(path, fm)
        all_rozsudky.append((rel, path, fm, forma, year_val))

    all_rozsudky_rel_paths = {rel for rel, _, _, _, _ in all_rozsudky}
    tracked: dict = state.setdefault("files", {})

    # 2. Mažeme ze Storu POUZE usnesení nebo soubory fyzicky smazané z repozitáře (NIKDY starší rozsudky)
    remote_by_rel, remote_by_title = fetch_existing_remote_docs(client, store_name)
    removed_paths = [rel for rel in list(tracked.keys()) if rel not in all_rozsudky_rel_paths]
    already_deleted: set[str] = set()

    for idx, rel in enumerate(removed_paths, 1):
        doc_name = tracked[rel].get("document_name", "")
        if idx <= 10 or idx % 50 == 0:
            print(f"Odstraňuji z RAG (usnesení / smazaný soubor): {rel} [{idx}/{len(removed_paths)}]")
        if doc_name and doc_name not in already_deleted:
            delete_remote_document(client, doc_name)
            already_deleted.add(doc_name)
        del tracked[rel]

    for rel, doc_name in remote_by_rel.items():
        if rel not in all_rozsudky_rel_paths and doc_name not in already_deleted:
            delete_remote_document(client, doc_name)
            already_deleted.add(doc_name)

    if removed_paths:
        save_state(state)

    # 3. Převzetí všech už nahraných rozsudků (včetně 2010–2014) + nahrání chybějících od min_year
    uploaded = 0
    skipped = 0
    adopted = 0
    deferred_older = 0
    consecutive_timeouts = 0

    for rel, path, fm, resolved_forma, year_val in all_rozsudky:
        digest = sha256_file(path)
        prev = tracked.get(rel)

        # Už evidováno v indexu beze změny -> ponechat
        if not force_resync and prev and prev.get("sha256") == digest and prev.get("document_name"):
            skipped += 1
            continue

        display_name, custom_metadata = build_metadata(path, fm, resolved_forma, year_val)

        # Pokud už rozsudek ve Storu leží (i z let 2010–2014), převezmeme ho do indexu bez nahrávání
        if not prev and not force_resync:
            existing_doc = remote_by_rel.get(rel) or remote_by_title.get(display_name)
            if existing_doc:
                tracked[rel] = {
                    "sha256": digest,
                    "display_name": display_name,
                    "document_name": existing_doc,
                }
                adopted += 1
                continue

        # Starší dosud nenahrané rozsudky před min_year jen přeskočíme pro pozdější doindexaci
        if min_year > 0 and year_val > 0 and year_val < min_year:
            deferred_older += 1
            continue

        if max_uploads > 0 and uploaded >= max_uploads:
            print(f"Dosažen limit dávky MAX_UPLOADS={max_uploads}. Ukládám index, zbytek proběhne v dalším běhu.")
            break

        if prev and prev.get("document_name"):
            delete_remote_document(client, prev["document_name"])

        print(f"[{uploaded + 1}] Indexuji celý rozsudek: {rel} -> '{display_name}'")
        try:
            doc_name = upload_document(client, store_name, path, display_name, custom_metadata)
            consecutive_timeouts = 0
        except TimeoutError as exc:
            consecutive_timeouts += 1
            print(f"  ⏳ Timeout ({consecutive_timeouts}/3): {exc}")
            if consecutive_timeouts >= 3:
                print("  ⚠️ Google indexovací fronta je přetížená – ukládám dosavadní postup.")
                break
            time.sleep(15)
            continue
        except Exception as exc:
            msg = str(exc)
            if "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower() or "storage" in msg.lower():
                print(f"  🛑 Dosažen limit kapacity nebo kvóty Google File Search: {exc}")
                break
            print(f"  ❌ Přeskakuji {rel}: {exc}")
            continue

        tracked[rel] = {
            "sha256": digest,
            "display_name": display_name,
            "document_name": doc_name,
        }
        uploaded += 1
        time.sleep(0.5)

        if uploaded % 25 == 0:
            save_state(state)

    save_state(state)
    print(
        f"Hotovo | Nahrávání od roku: {min_year} | Vyřazeno usnesení: {excluded_usneseni} | "
        f"Odloženo starších (před {min_year}): {deferred_older} | Smazáno ze Storu: {len(removed_paths)} | "
        f"Převzato ze Storu: {adopted} | Nahráno v dávce: {uploaded} | "
        f"Beze změny: {skipped} | Celkem rozsudků v RAG: {len(tracked)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CHYBA: {exc}", file=sys.stderr)
        sys.exit(1)
