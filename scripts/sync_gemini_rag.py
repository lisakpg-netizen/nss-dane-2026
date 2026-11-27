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

# 0 = indexovat všechny ročníky rozsudků v repozitáři (lze přepsat přes proměnnou MIN_YEAR)
DEFAULT_MIN_YEAR = 0

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


def is_rozsudek_only(path: Path, fm: dict[str, str], body: str, min_year: int) -> bool:
    """Vrátí True pouze pro neprázdné rozsudky (vyřadí veškerá usnesení)."""
    if len(body.strip()) < 100:
        return False

    # 1. Kontrola formy v YAML hlavičce
    forma = fm.get("forma", "").strip().lower()
    if "usnesení" in forma or "usneseni" in forma:
        return False

    # 2. Pojistka podle nadpisu v záhlaví textu (pro případy, kdy YAML forma chybí)
    if re.search(
        r"(?:^|\n)\s*(?:#+\s*)?u\s*s\s*n\s*e\s*s\s*e\s*n\s*[íi]\b",
        body[:2500],
        re.IGNORECASE,
    ):
        return False

    # 3. Volitelný filtr podle roku (výchozí 0 = všechny roky)
    if min_year > 0:
        rel_parts = path.relative_to(ROOT_DIR).parts
        year_val = 0
        if len(rel_parts) >= 4 and rel_parts[2].isdigit():
            year_val = int(rel_parts[2])
        elif fm.get("rok_archivace", "").isdigit():
            year_val = int(fm["rok_archivace"])
        else:
            m = re.search(
                r"\b(20\d{2})\b",
                fm.get("datum_rozhodnuti", "") + " " + fm.get("spisova_znacka", ""),
            )
            if m:
                year_val = int(m.group(1))
        if year_val > 0 and year_val < min_year:
            return False

    return True


def collect_candidate_files() -> list[Path]:
    if not JUDIKATURA_DIR.exists():
        return []
    return sorted(JUDIKATURA_DIR.rglob("*.md"))


def build_metadata(path: Path, fm: dict[str, str]) -> tuple[str, list[dict[str, str]]]:
    rel = path.relative_to(ROOT_DIR).as_posix()
    parts = rel.split("/")
    rejstrik_folder = parts[1].lower() if len(parts) >= 3 else "afs"
    year_folder = parts[2] if len(parts) >= 4 and parts[2].isdigit() else ""

    spzn = fm.get("spisova_znacka", "").strip() or path.stem.replace("NSS_", "").replace("_", " ")
    ecli = fm.get("ecli", "").strip()
    datum = fm.get("datum_rozhodnuti", "").strip()
    forma = fm.get("forma", "Rozsudek").strip()
    rejstrik = fm.get("rejstrik", rejstrik_folder).strip().lower()

    rok = fm.get("rok_archivace", "").strip() or year_folder
    if not rok:
        m_year = re.search(r"\b(20\d{2})\b", datum) or re.search(r"/(20\d{2})\b", spzn)
        rok = m_year.group(1) if m_year else ""

    slug = re.sub(r"[^a-z0-9]+", "-", spzn.lower()).strip("-") or path.stem.lower()
    display_name = (
        f"NSS {spzn} ({forma}, {datum})" if datum else f"NSS {spzn} ({forma})"
    )

    raw_meta: dict[str, str] = {
        "source_repo": SOURCE_REPO,
        "slug": slug,
        "category": rejstrik,
        "doc_type": DEFAULT_DOC_TYPE,
        "authority": DEFAULT_AUTHORITY,
        "spisova_znacka": spzn,
        "ecli": ecli,
        "forma": forma,
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
        print(f"  Varování: nepodařilo se smazat {doc_name}: {exc}")


def upload_document(
    client: genai.Client,
    store_name: str,
    path: Path,
    display_name: str,
    custom_metadata: list[dict[str, str]],
) -> str:
    # Kopie celého souboru pod čistě ASCII názvem kvůli HTTP hlavičce v google-genai SDK
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
    max_uploads = int(os.getenv("MAX_UPLOADS", "200") or "200")
    env_year = os.getenv("MIN_YEAR", "").strip()
    min_year = int(env_year) if env_year.isdigit() and int(env_year) > 0 else DEFAULT_MIN_YEAR

    client = get_client()
    state = load_state()
    store_name = get_or_create_store(client, state)
    state["store_name"] = store_name
    state["store_display_name"] = STORE_DISPLAY_NAME

    # 1. Výběr pouze rozsudků (celé texty, bez usnesení)
    eligible_items: list[tuple[Path, dict[str, str]]] = []
    excluded_count = 0
    for path in collect_candidate_files():
        raw_text = path.read_text("utf-8", errors="replace")
        fm, body = parse_nss_frontmatter(raw_text)
        if not is_rozsudek_only(path, fm, body, min_year=min_year):
            excluded_count += 1
            continue
        eligible_items.append((path, fm))

    eligible_rel_paths = {p.relative_to(ROOT_DIR).as_posix() for p, _ in eligible_items}
    tracked: dict = state.setdefault("files", {})

    # 2. Promazání dříve nahraných usnesení ze Storu i z lokálního indexu
    remote_by_rel, remote_by_title = fetch_existing_remote_docs(client, store_name)
    removed_paths = [rel for rel in list(tracked.keys()) if rel not in eligible_rel_paths]
    for idx, rel in enumerate(removed_paths, 1):
        if idx <= 10 or idx % 50 == 0:
            print(f"Odstraňuji z RAG (usnesení / mimo filtr): {rel} [{idx}/{len(removed_paths)}]")
        delete_remote_document(client, tracked[rel].get("document_name", ""))
        del tracked[rel]

    for rel, doc_name in remote_by_rel.items():
        if rel not in eligible_rel_paths:
            delete_remote_document(client, doc_name)

    if removed_paths:
        save_state(state)

    # 3. Nahrání chybějících celých rozsudků
    uploaded = 0
    skipped = 0
    adopted = 0
    consecutive_timeouts = 0

    for path, fm in eligible_items:
        rel = path.relative_to(ROOT_DIR).as_posix()
        digest = sha256_file(path)
        prev = tracked.get(rel)

        if not force_resync and prev and prev.get("sha256") == digest and prev.get("document_name"):
            skipped += 1
            continue

        display_name, custom_metadata = build_metadata(path, fm)

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
        f"Hotovo | Vyřazeno usnesení: {excluded_count} | Smazáno ze Storu: {len(removed_paths)} | "
        f"Převzato ze Storu: {adopted} | Nahráno v dávce: {uploaded} | "
        f"Beze změny: {skipped} | Celkem rozsudků v RAG: {len(tracked)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"CHYBA: {exc}", file=sys.stderr)
        sys.exit(1)
