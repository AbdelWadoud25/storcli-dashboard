
import streamlit as st
import os
import zipfile
from io import BytesIO
import re
from dataclasses import dataclass

# Optional RAR support (only if library + external tool exist)
try:
    import rarfile  # type: ignore
    RAR_AVAILABLE = True
except Exception:
    rarfile = None  # type: ignore
    RAR_AVAILABLE = False

# ------------------------------------------------------------
# Config via environment variables (dynamic behavior)
# ------------------------------------------------------------
@dataclass
class AppConfig:
    TEXT_ENCODING: str = os.getenv("STORCLI_TEXT_ENCODING", "utf-8")
    MAX_ZIP_SIZE_MB: int = int(os.getenv("STORCLI_MAX_ZIP_MB", "200"))
    MAX_NESTED_DEPTH: int = int(os.getenv("STORCLI_MAX_NESTED_DEPTH", "3"))
    ENABLE_RAR: bool = os.getenv("STORCLI_ENABLE_RAR", "0").lower() in ("1", "true", "yes")
    ES_URL: str = os.getenv("STORCLI_ES_URL", "")
    ES_API_KEY: str = os.getenv("STORCLI_ES_API_KEY", "")
    TIMEOUT_SEC: int = int(os.getenv("STORCLI_TIMEOUT_SEC", "25"))

CONFIG = AppConfig()

# ------------------------------------------------------------
# Friendly titles mapping
# ------------------------------------------------------------
file_titles = {
    "mr_events.txt": "Physical Drive Events",
    "show_all.txt": "RAID Controller Properties, VD LIST and PD LIST",
    "bbu_show_all.txt": "BBU Status",
    "dall_show_all.txt": "Total VD & PD",
    "cv_show_all.txt": "Cachevault/SuperAP Status",
    "mr_events0.txt": "Event Log",
    "eall_show_all.txt": "Enclosure Inventory and Status Report",
    "vall_show_all.txt": "VD Status and Properties",
    "sall_show_all.txt": "Detailed Drives Info",
    "termlog.txt": "RAID Controller Ops and Errors Detection",
}

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def normalize_filename(filename: str) -> str:
    """Normalize StorCLI filenames across common vendor/packaging variants.
    Keeps existing logic (strip controller prefix like c0_, c1-) and adds:
    - case-insensitive normalization
    - converts common CamelCase forms like BBUShowAll.txt -> bbu_show_all.txt
    - normalizes separators '-' to '_'
    - supports common mr_events naming typos/variants (mr_eventso vs mr_events0)
    """
    base = os.path.basename(filename)
    # Strip controller prefix (c0_, c1-, c2 etc.)
    base = re.sub(r"^c\d+[_-]?", "", base, flags=re.IGNORECASE)
    base = base.strip()
    base = base.replace("-", "_")
    low = base.lower()

    # Common CamelCase / no-underscore variants
    # Examples: BBUShowAll.txt, CVShowAll.txt, DALLShowAll.txt, EALLShowAll.txt, SALLShowAll.txt, VALLShowAll.txt
    m = re.fullmatch(r"(bbu|cv|dall|eall|sall|vall)showall\.txt", low)
    if m:
        return f"{m.group(1)}_show_all.txt"

    # ShowAll.txt -> show_all.txt
    if low == "showall.txt":
        return "show_all.txt"

    # mr_events variants (including frequent confusion between '0' and 'o')
    if re.fullmatch(r"(mr_?events0|mrevents0|mr_eventso|mreventso)\.txt", low):
        return "mr_events0.txt"
    if re.fullmatch(r"(mr_?events|mrevents)\.txt", low):
        return "mr_events.txt"

    # termlog variants
    if re.fullmatch(r"term_?log\.txt", low):
        return "termlog.txt"

    return low


def detect_controller_id(path: str) -> str:
    """Best-effort extraction of controller id (c0, c1, ...)
    from a member path like 'c0/show_all.txt', 'c1-showall.txt', or nested 'c0.zip::show_all.txt'.
    Returns 'unknown' if not detected.
    """
    p = path.replace("\\", "/").lower()
    # pattern 1: ::c0.zip:: or /c0/ or leading c0_
    m = re.search(r"(?:^|/|::)(c\d+)(?:[_\-/.]|::)", p)
    if m:
        return m.group(1)
    # pattern 2: .../c0/... without separator captured above
    m = re.search(r"/(c\d+)(?:/|$)", p)
    if m:
        return m.group(1)
    return "unknown"


def state_map(value: str) -> str:
    """Map StorCLI short states to normalized states.

    IMPORTANT: Do not collapse Unconfigured Good/Bad to Online/Failed.
    Keep them explicit as requested.
    """
    if not value:
        return None  # type: ignore
    v = value.strip().lower()
    mapping = {
        "optl": "Optimal",
        "onln": "Online",
        "dgrd": "Degraded",
        "offln": "Offline",
        # Unconfigured states must be preserved
        "ugood": "Unconfigured Good",
        "unconfigured good": "Unconfigured Good",
        "ubad": "Unconfigured Bad",
        "unconfigured bad": "Unconfigured Bad",
        # Hot spares / rebuild
        "dhs": "Dedicated Hot Spare",
        "ghs": "Global Hot Spare",
        "rbld": "Rebuild",
        "rebuild": "Rebuild",
        # Failures
        "fail": "Failed",
        "failed": "Failed",
        "missing": "Missing",
    }
    return mapping.get(v, value.strip())


def size_to_gb(num_str: str, unit: str):
    try:
        num = float(num_str)
    except Exception:
        return None
    unit = (unit or "").strip().upper()
    if unit == "TB":
        return round(num * 1024, 2)
    if unit == "GB":
        return round(num, 2)
    return None


def extract_section(text: str, start_markers, end_markers=None):
    """Extract a block of text after a marker until an end marker or EOF.

    NOTE: This returns the FIRST matching block. When we need per-controller
    parsing, we call this against each controller's text separately.
    """
    if not text:
        return ""
    low = text.lower()
    start_idx = -1
    for m in start_markers:
        i = low.find(m.lower())
        if i != -1:
            start_idx = i
            break
    if start_idx == -1:
        return ""
    sub = text[start_idx:]
    if end_markers:
        sub_low = sub.lower()
        end_positions = []
        for e in end_markers:
            j = sub_low.find(e.lower())
            if j != -1:
                end_positions.append(j)
        if end_positions:
            sub = sub[: min(end_positions)]
    return sub


def find_first(patterns, text, flags=re.IGNORECASE):
    if not text:
        return None
    for pat in patterns:
        m = re.search(pat, text, flags)
        if m:
            return m.group(1).strip() if m.groups() else m.group(0).strip()
    return None


def normalize_yes_no(v: str):
    if v is None:
        return None
    x = v.strip().lower()
    if x in ("yes", "y", "true"):
        return "Yes"
    if x in ("no", "n", "false"):
        return "No"
    return v.strip()


def dedupe_vds(vds: list) -> list:
    """
    Remove duplicate VD entries that represent the same VD but appear with different names.
    Example duplicates: 'DG0' vs 'DG0-Arr0'.
    Dedup key = (dg, raid_level, state, size_gb)
    Preference: keep 'DG<digit>' style name if present. Otherwise keep the shortest name.
    """
    if not vds:
        return []
    grouped = {}
    for vd in vds:
        key = (
            vd.get("dg"),
            (vd.get("raid_level") or "").strip(),
            (vd.get("state") or "").strip(),
            vd.get("size_gb"),
        )
        grouped.setdefault(key, []).append(vd)
    result = []
    for _key, items in grouped.items():
        preferred = None
        for it in items:
            name = (it.get("name") or "").strip()
            if re.fullmatch(r"DG\d+", name, flags=re.IGNORECASE):
                preferred = it
                break
        if not preferred:
            preferred = sorted(
                items,
                key=lambda x: (
                    len((x.get("name") or "").strip()) or 9999,
                    (x.get("name") or ""),
                ),
            )[0]
        result.append(preferred)
    return result

# ------------------------------------------------------------
# Parsers
# ------------------------------------------------------------

def parse_controller_info(show_all_text: str) -> dict:
    """Parse controller info from show_all.txt."""
    if not show_all_text:
        return {}
    return {
        "model": find_first([r"Model\s*=\s*(.+)"], show_all_text),
        "status": find_first([r"Controller Status\s*=\s*(Optimal|Degraded|Failed|Critical)"], show_all_text),
        "driver_name": find_first([r"Driver Name\s*=\s*(.+)"], show_all_text),
        "driver_version": find_first([r"Driver Version\s*=\s*(.+)"], show_all_text),
        "firmware": find_first([r"Firmware Version\s*=\s*(.+)"], show_all_text),
    }


def parse_cachevault_or_bbu(cv_text: str, bbu_text: str) -> dict:
    """
    Parse CacheVault/BBU information.
    IMPORTANT:
    - If logs report "Cachevault is absent!" or "Battery is absent!", KEEP the phrase.
    - Treat it as installed=False (informational, typically means not installed).
    """

    def _absent(unit_type: str, phrase: str):
        return {
            "type": unit_type,
            "status": phrase,
            "replacement_required": None,
            "installed": False,
        }

    def _extract_absent_phrase(text: str, default_phrase: str) -> str:
        m = re.search(r"(Cachevault\s+is\s+absent!|Battery\s+is\s+absent!)", text, re.IGNORECASE)
        if m:
            return m.group(1)
        if re.search(r"not\s+present", text, re.IGNORECASE):
            return default_phrase.replace("absent!", "not present")
        return default_phrase

    # CacheVault
    if cv_text:
        if re.search(r"cachevault\s+(is\s+absent!|not\s+present|absent)", cv_text, re.IGNORECASE):
            return _absent("CacheVault", _extract_absent_phrase(cv_text, "Cachevault is absent!"))
        top_status = find_first([r"Status\s*=\s*(\w+)"], cv_text)
        replacement_required = normalize_yes_no(find_first([r"Replacement required\s*=\s*(Yes|No)"], cv_text))
        stt = (top_status or "").strip().lower()
        if stt in ("failure", "failed"):
            status = "Failed"
        elif stt in ("success", "optimal", "ok"):
            status = "Optimal"
        else:
            status = top_status or "Unknown"
        return {"type": "CacheVault", "status": status, "replacement_required": replacement_required, "installed": True}

    # BBU
    if bbu_text:
        if (
            re.search(r"(battery|bbu)\s+(is\s+absent!|not\s+present|absent)", bbu_text, re.IGNORECASE)
            or re.search(r"no\s+battery\s+present", bbu_text, re.IGNORECASE)
        ):
            return _absent("BBU", _extract_absent_phrase(bbu_text, "Battery is absent!"))
        top_status = find_first([r"Status\s*=\s*(.+)"], bbu_text)
        replacement_required = normalize_yes_no(
            find_first([r"Replacement required\s*=\s*(Yes|No)"], bbu_text)
            or find_first([r"Battery Replacement.*:\s*(Yes|No)"], bbu_text)
        )
        stt = (top_status or "").strip().lower()
        if stt in ("optimal", "ok", "success"):
            status = "Optimal"
        elif "charg" in stt:
            status = "Charging"
        elif stt in ("failed", "failure"):
            status = "Failed"
        else:
            status = top_status
        return {"type": "BBU", "status": status, "replacement_required": replacement_required, "installed": True}

    return {"type": "None", "status": None, "replacement_required": None, "installed": None}


def parse_vd_list_token_based(text: str) -> list:
    """Robust VD LIST parser using tokens."""
    if not text:
        return []
    sec = extract_section(
        text,
        ["VD LIST"],
        end_markers=["DG Drive LIST", "TOPOLOGY", "DG Drive", "Drive LIST", "Total Drive Count"],
    )
    if not sec:
        return []
    vds = []
    for line in sec.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or "DG/VD" in line or "Total VD Count" in line:
            continue
        if not re.match(r"^\d+/\d+\s+", line):
            continue
        tokens = line.split()
        if len(tokens) < 6:
            continue
        dgvd = tokens[0]
        raid_type = tokens[1]
        state_raw = tokens[2]
        name = tokens[-1]
        unit = tokens[-2]
        size_num = tokens[-3]
        size_gb = size_to_gb(size_num, unit)
        dg, vd = dgvd.split("/")
        vds.append(
            {
                "dg": int(dg),
                "vd_id": int(vd),
                "raid_level": raid_type,
                "state": state_map(state_raw),
                "name": name,
                "size_gb": size_gb,
            }
        )
    return vds


def parse_vds_from_topology_fallback(dall_text: str) -> list:
    """Fallback VD extraction from DALL TOPOLOGY RAID rows if VD LIST missing."""
    if not dall_text:
        return []
    topo = extract_section(dall_text, ["TOPOLOGY"], end_markers=["VD LIST", "DG Drive LIST", "Total Drive Count"])
    if not topo:
        return []
    vds = []
    for line in topo.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("DG "):
            continue
        m = re.search(
            r"^(?P<dg>\d+)\s+(?P<arr>\d+|-)\s+(?P<row>\d+|-)\s+"
            r"(?P<eid>-|\d+:\d+)\s+(?P<did>-|\d+)\s+"
            r"(?P<type>RAID\d+)\s+(?P<state>\S+)\s+\S+\s+"
            r"(?P<size>\d+(?:\.\d+)?)\s+(?P<unit>TB|GB)\b",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        if m.group("eid") != "-":
            continue
        dg = int(m.group("dg"))
        arr = m.group("arr")
        raid_type = m.group("type")
        state_raw = m.group("state")
        size_gb = size_to_gb(m.group("size"), m.group("unit"))
        vd_name = f"DG{dg}-Arr{arr}" if arr != "-" else f"DG{dg}"
        vds.append(
            {
                "dg": dg,
                "vd_id": None,
                "raid_level": raid_type,
                "state": state_map(state_raw),
                "name": vd_name,
                "size_gb": size_gb,
            }
        )
    uniq, seen = [], set()
    for vd in vds:
        key = (vd.get("dg"), vd.get("name"), vd.get("raid_level"), vd.get("size_gb"), vd.get("state"))
        if key not in seen:
            seen.add(key)
            uniq.append(vd)
    return uniq


def parse_topology_pds_from_dall(dall_text: str) -> list:
    """Parse PDs from DALL TOPOLOGY table using DRIVE rows."""
    if not dall_text:
        return []
    topo = extract_section(dall_text, ["TOPOLOGY"], end_markers=["VD LIST", "DG Drive LIST", "Total Drive Count"])
    if not topo:
        return []
    pds = []
    for line in topo.splitlines():
        raw = line
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("DG "):
            continue
        m = re.search(
            r"^(?P<dg>\d+)\s+(?P<arr>\d+|-)\s+(?P<row>\d+|-)\s+"
            r"(?P<eid>\d+:\d+|-)\s+(?P<did>\d+|-)\s+"
            r"(?P<type>DRIVE|RAID\d+)\s+(?P<state>\S+)\s+\S+\s+"
            r"(?P<size>\d+(?:\.\d+)?)\s+(?P<unit>TB|GB)\b",
            line,
            re.IGNORECASE,
        )
        if not m or m.group("type").upper() != "DRIVE":
            continue
        eid = m.group("eid")
        did = m.group("did")
        pds.append(
            {
                "slot": eid,
                "did": int(did) if did.isdigit() else None,
                "dg": int(m.group("dg")),
                "state": state_map(m.group("state")),
                "size_gb": size_to_gb(m.group("size"), m.group("unit")),
                "media_error_count": None,
                "other_error_count": None,
                "predictive_failure_count": None,
                "smart_alert": None,
                "temperature_c": None,
                "evidence_line": raw.strip(),
            }
        )
    return pds


def parse_sall_drive_counters(sall_text: str) -> dict:
    """Parse SALL per-drive blocks and return counters keyed by EID:Slot."""
    if not sall_text:
        return {}
    counters = {}
    blocks = re.split(r"(?=Drive\s+/c\d+/e\d+/s\d+\s+State\s*:)", sall_text, flags=re.IGNORECASE)
    for blk in blocks:
        blk = blk.strip()
        if not blk.lower().startswith("drive /c"):
            continue
        mh = re.search(
            r"Drive\s+/c\d+/e(?P<eid>\d+)/s(?P<slt>\d+)\s+State\s*:",
            blk,
            re.IGNORECASE,
        )
        if not mh:
            continue
        eid_slot = f"{mh.group('eid')}:{mh.group('slt')}"
        media = find_first([r"Media Error Count\s*=\s*(\d+)"], blk)
        other = find_first([r"Other Error Count\s*=\s*(\d+)"], blk)
        pred = find_first([r"Predictive Failure Count\s*=\s*(\d+)"], blk)
        smart = find_first([r"S\.M\.A\.R\.T\s*alert\s*flagged\s*by\s*drive\s*=\s*(Yes|No)"], blk)
        temp = find_first([r"Drive Temperature\s*=\s*(\d+)\s*C"], blk)
        counters[eid_slot] = {
            "media_error_count": int(media) if media is not None else None,
            "other_error_count": int(other) if other is not None else None,
            "predictive_failure_count": int(pred) if pred is not None else None,
            "smart_alert": smart,
            "temperature_c": int(temp) if temp is not None else None,
        }
    return counters


def merge_pd_counters(pds: list, counter_map: dict) -> list:
    """Merge PD topology info with SALL counters by EID:Slot."""
    if not pds:
        pds = [{"slot": k} for k in counter_map.keys()]
    for pd in pds:
        slot = pd.get("slot")
        if slot and slot in counter_map:
            pd.update(counter_map[slot])
    return pds

# ------------------------------------------------------------
# Archive Extractors (ZIP + optional RAR) with nested support
# ------------------------------------------------------------

def _extract_txt_from_zip(zip_bytes: bytes, ctx=None, depth: int = 0):
    """Extract .txt files from a zip, including nested zips (common in StorCLI bundles)."""
    if ctx is None:
        ctx = []
    if depth > CONFIG.MAX_NESTED_DEPTH:
        return []
    extracted = []
    with zipfile.ZipFile(BytesIO(zip_bytes)) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            ml = member.lower()
            if ml.endswith(".txt"):
                with z.open(member) as f:
                    content = f.read().decode(CONFIG.TEXT_ENCODING, errors="replace")
                label = "::".join([*ctx, member]) if ctx else member
                extracted.append((label, content))
            elif ml.endswith(".zip"):
                with z.open(member) as f:
                    nested_bytes = f.read()
                extracted.extend(_extract_txt_from_zip(nested_bytes, ctx=[*ctx, os.path.basename(member)], depth=depth+1))
            elif ml.endswith(".rar") and CONFIG.ENABLE_RAR and RAR_AVAILABLE:
                # Nested RAR inside ZIP
                with z.open(member) as f:
                    nested_bytes = f.read()
                extracted.extend(_extract_txt_from_rar(nested_bytes, ctx=[*ctx, os.path.basename(member)], depth=depth+1))
    return extracted


def _extract_txt_from_rar(rar_bytes: bytes, ctx=None, depth: int = 0):
    """Best-effort .rar extractor using `rarfile` if available. Supports nested zip/rar."""
    if not (CONFIG.ENABLE_RAR and RAR_AVAILABLE and rarfile):
        return []
    if ctx is None:
        ctx = []
    if depth > CONFIG.MAX_NESTED_DEPTH:
        return []
    extracted = []
    try:
        with rarfile.RarFile(BytesIO(rar_bytes)) as rf:  # type: ignore
            for member in rf.infolist():
                name = member.filename
                if name.endswith("/"):
                    continue
                ml = name.lower()
                if ml.endswith(".txt"):
                    with rf.open(name) as f:  # requires unrar/bsdtar in environment
                        content = f.read().decode(CONFIG.TEXT_ENCODING, errors="replace")
                    label = "::".join([*ctx, name]) if ctx else name
                    extracted.append((label, content))
                elif ml.endswith(".zip"):
                    with rf.open(name) as f:
                        nested_bytes = f.read()
                    extracted.extend(_extract_txt_from_zip(nested_bytes, ctx=[*ctx, os.path.basename(name)], depth=depth+1))
                elif ml.endswith(".rar"):
                    with rf.open(name) as f:
                        nested_bytes = f.read()
                    extracted.extend(_extract_txt_from_rar(nested_bytes, ctx=[*ctx, os.path.basename(name)], depth=depth+1))
    except Exception as e:
        st.warning(f"RAR extraction failed ({e}). Ensure 'rarfile' is installed and system has 'unrar' or 'bsdtar'.")
    return extracted

# ------------------------------------------------------------
# STREAMLIT UI (Viewer + Search)
# ------------------------------------------------------------

st.set_page_config(page_title="StorCLI Log Dashboard (v5)", layout="wide")
st.title("📊 StorCLI Log Dashboard (v5)")
st.markdown("Upload StorCLI `.txt` files or a `.zip`/`.rar` archive. Search logs and view a clean Summary per controller at the end.")

uploaded_zip = st.file_uploader("📦 Upload a ZIP/RAR file with StorCLI `.txt` logs", type=["zip", "rar"])  # RAR optional
uploaded_files = st.file_uploader("📄 Or upload individual `.txt` files", type=["txt"], accept_multiple_files=True)

all_files = []  # list of (original_filename, content)

# Handle ZIP/RAR (safe + nested zip support)
if uploaded_zip:
    up_name = (uploaded_zip.name or "").lower()
    up_size = getattr(uploaded_zip, "size", None)
    if up_size and up_size > CONFIG.MAX_ZIP_SIZE_MB * 1024 * 1024:
        st.error(f"Archive is larger than {CONFIG.MAX_ZIP_SIZE_MB} MB. Please split or increase STORCLI_MAX_ZIP_MB.")
    else:
        try:
            raw_bytes = uploaded_zip.read()

            if up_name.endswith(".rar"):
                if CONFIG.ENABLE_RAR and RAR_AVAILABLE:
                    with st.spinner("Extracting .rar (experimental)..."):
                        all_files.extend(_extract_txt_from_rar(raw_bytes))
                        if not all_files:
                            st.warning("No .txt files were found inside the uploaded RAR (including nested archives).")
                else:
                    if CONFIG.ENABLE_RAR and not RAR_AVAILABLE:
                        st.warning("RAR requested but dependencies missing. Install `rarfile` and system `unrar`/`bsdtar`.")
                    else:
                        st.warning(
                            "RAR files are not supported in this tool. "
                            "Please extract the .rar archive manually using WinRAR or 7-Zip, "
                            "then upload the extracted .txt StorCLI log files to the app."
                        )
            else:
                with st.spinner("Extracting .zip (with nested zips)..."):
                    all_files.extend(_extract_txt_from_zip(raw_bytes))
                    if not all_files:
                        st.warning("No .txt files were found inside the uploaded ZIP (including nested archives).")
        except zipfile.BadZipFile:
            st.error("The uploaded file is not a valid ZIP archive. If you have a StorCLI bundle in another format, please re-zip it or upload the .txt files directly.")
        except Exception as e:
            st.error(f"Failed to read the archive: {e}")

# Handle individual TXT
if uploaded_files:
    for file in uploaded_files:
        try:
            content = file.read().decode(CONFIG.TEXT_ENCODING, errors="replace")
        except Exception:
            content = file.read().decode("utf-8", errors="replace")
        all_files.append((file.name, content))

search_term = st.text_input("🔍 Search logs by keyword (e.g., SAS, FRU, medium error, predictive):").strip()

# Index logs by normalized filename for parsing (keep ALL variants; do not overwrite)
log_index = {}
for fname, content in all_files:
    n = normalize_filename(fname)
    log_index.setdefault(n, []).append(content)

# Also, group files by controller id to render per-controller summaries
ctrl_index = {}
for fname, content in all_files:
    ctrl = detect_controller_id(fname)
    n = normalize_filename(fname)
    ctrl_index.setdefault(ctrl, {})
    ctrl_index[ctrl].setdefault(n, []).append(content)


# ---- Log display with search filter ----
for filename, content in all_files:
    normalized_name = normalize_filename(filename)
    title = file_titles.get(normalized_name, f"Unknown File: {filename}")
    lines = content.splitlines()
    filtered_lines = [line for line in lines if search_term.lower() in line.lower()] if search_term else lines
    has_hits = bool(search_term) and len(filtered_lines) > 0
    hit_count = len(filtered_lines) if search_term else 0

    # Hide non-matching during search
    if search_term and not has_hits:
        continue

    # Green banner above matching section
    if search_term and has_hits:
        st.markdown(
            f"""
            <div style="
            padding: 8px 12px;
            margin: 6px 0 2px 0;
            background-color: #e8f5e9;
            border-left: 6px solid #2e7d32;
            border-radius: 6px;
            font-size: 14px;
            line-height: 1.35;">
            ✅ <b>Match found</b> in: <b>{title}</b>
            </div>
            """,
            unsafe_allow_html=True,
        )

    expander_title = f"📁 {title}"
    if search_term and has_hits:
        expander_title = f"📁 {title} — ✅ {hit_count} match(es)"
    with st.expander(expander_title):
        st.code("\n".join(filtered_lines), language="text")

# ------------------------------------------------------------
# Summary Panel (END) — CLEAN TEXT, now per controller
# ------------------------------------------------------------

st.markdown("---")
st.subheader("🧾 Summary")


def render_compact_section(header: str, lines: list[str]):
    """Compact section: heading + st.text (no extra blank spacing)."""
    st.markdown(f"### {header}")
    st.text("\n".join(lines))


if not all_files:
    st.info("Upload logs to generate the summary.")
else:
    def render_summary_for(ctrl: str, index_for_ctrl: dict):
        def get_log_text(norm_name: str) -> str:
            parts = index_for_ctrl.get(norm_name, [])
            return "\n\n".join(parts) if parts else ""

        show_all = get_log_text("show_all.txt")
        dall = get_log_text("dall_show_all.txt")
        vall = get_log_text("vall_show_all.txt")
        sall = get_log_text("sall_show_all.txt")
        cv = get_log_text("cv_show_all.txt")
        bbu = get_log_text("bbu_show_all.txt")

        controller = parse_controller_info(show_all)
        cache = parse_cachevault_or_bbu(cv, bbu)

        # VD parsing: prefer VD LIST; fallback to TOPOLOGY RAID rows
        vd_src = vall or dall or show_all
        vds = parse_vd_list_token_based(vd_src)
        if not vds and dall:
            vds = parse_vds_from_topology_fallback(dall)
        # Deduplicate VD entries
        vds = dedupe_vds(vds)

        degraded_states = {"degraded", "rebuild"}
        vd_summary = {
            "total_vds": len(vds),
            "degraded_vds": sum(1 for x in vds if (x.get("state") or "").lower() in degraded_states),
            "offline_vds": sum(1 for x in vds if (x.get("state") or "").lower() == "offline"),
        }

        # PD summary: DALL TOPOLOGY + SALL counters merge
        pds_from_dall = parse_topology_pds_from_dall(dall)
        counter_map = parse_sall_drive_counters(sall)
        pds = merge_pd_counters(pds_from_dall, counter_map)

        # Presentation-only: if user didn't provide DALL/SALL, show PD as N/A (avoid misleading 0)
        has_pd_sources = bool(dall.strip()) or bool(sall.strip())
        pd_summary = {
            "total_pds": len(pds),
            "failed_pds": sum(1 for x in pds if (x.get("state") or "").lower() in ("failed", "offline", "missing")),
            "predictive_failure_pds": sum(1 for x in pds if (x.get("predictive_failure_count") or 0) > 0),
            "media_error_pds": sum(1 for x in pds if (x.get("media_error_count") or 0) > 0),
            "other_error_pds": sum(1 for x in pds if (x.get("other_error_count") or 0) > 0),
        }

        # Header per controller
        ctrl_label = ctrl.upper() if ctrl != "unknown" else "UNKNOWN"
        st.markdown(f"#### ▶ Controller {ctrl_label}")

        # ---- Controller Summary (compact)
        dn = (controller.get("driver_name") or "").strip()
        dv = (controller.get("driver_version") or "").strip()
        driver_txt = f"{dn} ({dv})" if dn and dv else (dn or "N/A")
        render_compact_section(
            "🎛️ Controller Summary",
            [
                f"Model: {controller.get('model') or 'N/A'}",
                f"Firmware: {controller.get('firmware') or 'N/A'}",
                f"Driver: {driver_txt}",
                f"Status: {controller.get('status') or 'N/A'}",
            ],
        )

        # ---- VD / PD Summary (compact)
        if has_pd_sources:
            pd_line = (
                f"PDs: Total={pd_summary['total_pds']} "
                f"Failed/Offline={pd_summary['failed_pds']} "
                f"MediaErr>0={pd_summary['media_error_pds']} "
                f"OtherErr>0={pd_summary['other_error_pds']} "
                f"PredFail>0={pd_summary['predictive_failure_pds']}"
            )
        else:
            pd_line = "PDs: N/A (missing DALL/SALL)"
        render_compact_section(
            "🧩 VD / PD Summary",
            [
                f"VDs: Total={vd_summary['total_vds']} Degraded={vd_summary['degraded_vds']} Offline={vd_summary['offline_vds']}",
                pd_line,
            ],
        )

        # ---- Cache / Backup Unit (compact)
        if cache.get("type") and cache.get("type") != "None":
            render_compact_section(
                "🔋 Cache / Backup Unit",
                [
                    f"Type: {cache.get('type')}",
                    f"Status: {cache.get('status')}",
                ],
            )

        # ---- Virtual Drives
        st.markdown("### 🧱 Virtual Drives (VD)")
        if vds:
            st.text(
                "\n".join(
                    [
                        f"- {(vd.get('name') or 'VD')}: {(vd.get('raid_level') or 'N/A')} {(vd.get('state') or 'N/A')} "
                        f"{(str(vd.get('size_gb')) + ' GB') if vd.get('size_gb') is not None else 'N/A'}"
                        for vd in vds
                    ]
                )
            )
        else:
            st.text("- N/A (VD list not found in provided logs)")

        # ---- Physical Drives
        st.markdown("### 💽 Physical Drives (PD)")
        if not has_pd_sources:
            st.text("N/A (PD topology/counters not found: provide dall_show_all.txt and/or sall_show_all.txt for PD details).")
        else:
            header_line = (
                f"Total PD(s): {pd_summary['total_pds']} "
                f"Failed/Offline: {pd_summary['failed_pds']} "
                f"MediaErr>0: {pd_summary['media_error_pds']} "
                f"OtherErr>0: {pd_summary['other_error_pds']} "
                f"PredFail>0: {pd_summary['predictive_failure_pds']}"
            )
            abnormal = []
            for pd in pds:
                stt = (pd.get("state") or "").lower()
                media = pd.get("media_error_count") or 0
                other = pd.get("other_error_count") or 0
                pred = pd.get("predictive_failure_count") or 0
                smart = (pd.get("smart_alert") or "").lower()
                if stt in ("failed", "offline", "missing") or media > 0 or other > 0 or pred > 0 or smart == "yes":
                    abnormal.append(pd)
            if not abnormal:
                st.text(header_line + "\n" + "No abnormal PD counters/states detected (all counters are 0 and states are Online/Optimal/UGood where available).")
            else:
                lines = [header_line]
                for pd in abnormal:
                    media = pd.get("media_error_count") or 0
                    other = pd.get("other_error_count") or 0
                    pred = pd.get("predictive_failure_count") or 0
                    smart = pd.get("smart_alert") or "N/A"
                    temp = pd.get("temperature_c")
                    temp_str = f"{temp}C" if temp is not None else "N/A"
                    lines.append(
                        f"- {pd.get('slot')}: State={pd.get('state')}, MediaErr={media}, OtherErr={other}, PredFail={pred}, SMART={smart}, Temp={temp_str}"
                    )
                st.text("\n".join(lines))

        # Return summary dict for optional downstream export (e.g., Elasticsearch)
        return {
            "controller": ctrl_label,
            "controller_info": controller,
            "cache": cache,
            "vd_summary": vd_summary,
            "pd_summary": pd_summary,
        }

    # Render per-controller (ensure stable order: c0, c1, ..., unknown last)
    ordered_ctrls = sorted([c for c in ctrl_index.keys() if c != "unknown"], key=lambda x: int(x[1:]) if x[1:].isdigit() else 9999)
    if "unknown" in ctrl_index:
        ordered_ctrls.append("unknown")

    summaries = []
    for ctrl in ordered_ctrls:
        summaries.append(render_summary_for(ctrl, ctrl_index[ctrl]))

# Optional: export to Elasticsearch if configured
# Treat whitespace-only values as "not set"
_es_url = (CONFIG.ES_URL or "").strip()
_es_api_key = (CONFIG.ES_API_KEY or "").strip()

if _es_url:
    if st.button("📤 Send summary to Elasticsearch (experimental)"):
        try:
            import json, urllib.request
            payload = json.dumps({"summaries": summaries}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if _es_api_key:
                headers["Authorization"] = f"ApiKey { _es_api_key }"
            req = urllib.request.Request(_es_url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=CONFIG.TIMEOUT_SEC) as resp:
                st.success(f"Elasticsearch response: {resp.status}")
        except Exception as e:
            st.warning(f"Failed to send to Elasticsearch: {e}")
else:
    st.info("Elasticsearch export available: set STORCLI_ES_URL and (optionally) STORCLI_ES_API_KEY.")
