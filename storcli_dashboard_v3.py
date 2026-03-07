import streamlit as st
import os
import zipfile
from io import BytesIO
import re

# --------------------------------------------------
# Friendly titles mapping
# --------------------------------------------------
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

# --------------------------------------------------
# Helpers
# --------------------------------------------------

def normalize_filename(filename: str) -> str:
    """Supports filenames starting with c0_, c1-, c2..."""
    base = os.path.basename(filename)
    base = re.sub(r"^c\d+[_-]?", "", base, flags=re.IGNORECASE)
    return base


def state_map(value: str) -> str:
    """Map StorCLI short states to normalized states."""
    if not value:
        return None
    v = value.strip().lower()
    mapping = {
        "optl": "Optimal",
        "onln": "Online",
        "dgrd": "Degraded",
        "offln": "Offline",
        "ubad": "Failed",
        "ugood": "Online",
        "dhs": "Rebuild",
        "rbld": "Rebuild",
        "rebuild": "Rebuild",
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
    """Extract a block of text after a marker until an end marker or EOF."""
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
    """Remove duplicate VD entries that represent the same VD but appear with different names.

    Example duplicates: 'DG0' vs 'DG0-Arr0'.
    Dedup key = (dg, raid_level, state, size_gb)
    Preference (Option A): keep 'DG<digit>' style name if present.
    Otherwise keep the shortest non-empty name.
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
                key=lambda x: (len((x.get("name") or "").strip()) or 9999, (x.get("name") or "")),
            )[0]
        result.append(preferred)

    return result


# --------------------------------------------------
# Parsers
# --------------------------------------------------

def parse_controller_info(show_all_text: str) -> dict:
    """Parse controller info from show_all.txt."""
    if not show_all_text:
        return {}
    return {
        "model": find_first([r"Model\s*=\s*(.+)"], show_all_text),
        "status": find_first([r"Controller Status\s*=\s*(Optimal|Degraded|Failed)"], show_all_text),
        "driver_name": find_first([r"Driver Name\s*=\s*(.+)"], show_all_text),
        "driver_version": find_first([r"Driver Version\s*=\s*(.+)"], show_all_text),
        "firmware": find_first([r"Firmware Version\s*=\s*(.+)"], show_all_text),
    }


def parse_cachevault_or_bbu(cv_text: str, bbu_text: str) -> dict:
    """Parse CacheVault/BBU information.

    IMPORTANT:
    - If logs report "Cachevault is absent!" or "Battery is absent!", KEEP the text EXACTLY as-is.
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
        m = re.search(r"(Cachevault\s+is\s+absent!|Battery\s+is\s+absent!)", text)
        if m:
            return m.group(1)
        if re.search(r"not\s+present", text, re.IGNORECASE):
            return default_phrase.replace("absent!", "not present")
        return default_phrase

    # CacheVault
    if cv_text:
        if re.search(r"cachevault\s+is\s+absent!", cv_text, re.IGNORECASE) or re.search(r"cachevault\s+not\s+present|cachevault\s+absent", cv_text, re.IGNORECASE):
            return _absent("CacheVault", _extract_absent_phrase(cv_text, "Cachevault is absent!"))

        top_status = find_first([r"Status\s*=\s*(\w+)"], cv_text)
        replacement_required = normalize_yes_no(find_first([r"Replacement required\s+(Yes|No)"], cv_text))

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
        if re.search(r"battery\s+is\s+absent!", bbu_text, re.IGNORECASE) or re.search(r"battery\s+not\s+present|no\s+battery\s+present|bbu\s+is\s+absent|bbu\s+not\s+present", bbu_text, re.IGNORECASE):
            return _absent("BBU", _extract_absent_phrase(bbu_text, "Battery is absent!"))

        top_status = find_first([r"Status\s*=\s*(.+)"], bbu_text)
        replacement_required = normalize_yes_no(
            find_first([r"Replacement required\s+(Yes|No)"], bbu_text)
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

    # de-duplicate (within topology fallback)
    uniq = []
    seen = set()
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

        mh = re.search(r"Drive\s+/c\d+/e(?P<eid>\d+)/s(?P<slt>\d+)\s+State\s*:", blk, re.IGNORECASE)
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
    if not pds:
        pds = [{"slot": k} for k in counter_map.keys()]

    for pd in pds:
        slot = pd.get("slot")
        if slot and slot in counter_map:
            pd.update(counter_map[slot])

    return pds


# --------------------------------------------------
# STREAMLIT UI (Viewer + Search) — keep UX as implemented
# --------------------------------------------------

st.set_page_config(page_title="StorCLI Log Dashboard (v3)", layout="wide")
st.title("📊 StorCLI Log Dashboard (v3)")
st.markdown("Upload StorCLI `.txt` files or a `.zip` archive. Search logs and view a text-style Summary at the end.")

uploaded_zip = st.file_uploader("📦 Upload a ZIP file with StorCLI `.txt` logs", type=["zip"])
uploaded_files = st.file_uploader("📄 Or upload individual `.txt` files", type=["txt"], accept_multiple_files=True)

all_files = []  # list of (original_filename, content)

# Handle ZIP
if uploaded_zip:
    with zipfile.ZipFile(BytesIO(uploaded_zip.read())) as z:
        txt_files = [f for f in z.namelist() if f.lower().endswith(".txt")]
        for filename in txt_files:
            with z.open(filename) as file:
                content = file.read().decode("utf-8", errors="replace")
                all_files.append((filename, content))

# Handle individual TXT
if uploaded_files:
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="replace")
        all_files.append((file.name, content))

search_term = st.text_input("🔍 Search logs by keyword (e.g., SAS, FRU, medium error, predictive):").strip()

# Index logs by normalized filename for parsing (keep ALL variants; do not overwrite)
log_index = {}
for fname, content in all_files:
    n = normalize_filename(fname)
    log_index.setdefault(n, []).append(content)


def get_log_text(norm_name: str) -> str:
    """Return combined text for a normalized filename (handles multi-controller uploads)."""
    parts = log_index.get(norm_name, [])
    return "\n\n".join(parts) if parts else ""


# ---- Log display with your enhancements ----
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

    # Green banner above matching section (no match count to avoid repetition)
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


# --------------------------------------------------
# Summary Panel (END) — NORMAL TEXT (no JSON blocks)
# --------------------------------------------------

st.markdown("---")
st.subheader("🧾 Summary")

if not all_files:
    st.info("Upload logs to generate the summary.")
else:
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

    # Deduplicate VD entries (prefer DG0-style)
    vds = dedupe_vds(vds)

    degraded_states = {"degraded", "rebuild"}
    vd_summary = {
        "total_vds": len(vds),
        "degraded_vds": sum(1 for x in vds if (x.get("state") or "").lower() in degraded_states),
        "offline_vds": sum(1 for x in vds if (x.get("state") or "").lower() == "offline"),
        "vds": vds,
    }

    # PD summary: DALL TOPOLOGY + SALL counters merge
    pds_from_dall = parse_topology_pds_from_dall(dall)
    counter_map = parse_sall_drive_counters(sall)
    pds = merge_pd_counters(pds_from_dall, counter_map)

    pd_summary = {
        "total_pds": len(pds),
        "failed_pds": sum(1 for x in pds if (x.get("state") or "").lower() in ("failed", "offline", "missing")),
        "predictive_failure_pds": sum(1 for x in pds if (x.get("predictive_failure_count") or 0) > 0),
        "media_error_pds": sum(1 for x in pds if (x.get("media_error_count") or 0) > 0),
        "other_error_pds": sum(1 for x in pds if (x.get("other_error_count") or 0) > 0),
    }

    # ---------------- Controller Summary (Text) ----------------
    st.markdown("### 🎛️ Controller Summary")
    dn = (controller.get("driver_name") or "").strip()
    dv = (controller.get("driver_version") or "").strip()
    driver_txt = (f"{dn} ({dv})".strip() if dv else dn) or None

    st.write(f"\"model\":\"{controller.get('model') or 'N/A'}\"")
    st.write(f"\"firmware\":\"{controller.get('firmware') or 'N/A'}\"")
    st.write(f"\"driver\":\"{driver_txt or 'N/A'}\"")
    st.write(f"\"status\":\"{controller.get('status') or 'N/A'}\"")

    # ---------------- VD / PD Summary (Text) ----------------
    st.markdown("### 🧩 VD / PD Summary")

    st.write("\"vd_summary\":")
    st.write(f"\"total_vds\":{vd_summary.get('total_vds', 0)}")
    st.write(f"\"degraded_vds\":{vd_summary.get('degraded_vds', 0)}")
    st.write(f"\"offline_vds\":{vd_summary.get('offline_vds', 0)}")

    st.write("\"pd_summary\":")
    st.write(f"\"total_pds\":{pd_summary.get('total_pds', 0)}")
    st.write(f"\"failed_pds\":{pd_summary.get('failed_pds', 0)}")
    st.write(f"\"predictive_failure_pds\":{pd_summary.get('predictive_failure_pds', 0)}")
    st.write(f"\"media_error_pds\":{pd_summary.get('media_error_pds', 0)}")
    st.write(f"\"other_error_pds\":{pd_summary.get('other_error_pds', 0)}")

    # ---------------- Cache / Backup Unit ----------------
    if cache.get("type") and cache.get("type") != "None":
        st.markdown("**Cache / Backup Unit:**")
        st.write(f"- Type: {cache.get('type')}")
        st.write(f"- Status: {cache.get('status')}")

    # ---------------- Virtual Drives (VD) ----------------
    st.markdown("**Virtual Drives (VD):**")
    # Per request: remove the totals line here.
    if vds:
        for vd in vds:
            name = vd.get('name') or 'VD'
            raid = vd.get('raid_level') or 'N/A'
            stt = vd.get('state') or 'N/A'
            size = vd.get('size_gb')
            size_str = f"{size} GB" if size is not None else "N/A"
            st.write(f"- {name}: {raid} | {stt} | {size_str} | {name}")
    else:
        st.write("- N/A (VD list not found in provided logs)")

    # ---------------- Physical Drives (PD) ----------------
    st.markdown("**Physical Drives (PD):**")
    st.write(
        f"- Total PD(s): {pd_summary.get('total_pds', 0)} | Failed/Offline: {pd_summary.get('failed_pds', 0)} | "
        f"MediaErr>0: {pd_summary.get('media_error_pds', 0)} | OtherErr>0: {pd_summary.get('other_error_pds', 0)} | "
        f"PredFail>0: {pd_summary.get('predictive_failure_pds', 0)}"
    )

    # Abnormal-only PD lines
    abnormal = []
    for pd in pds:
        stt = (pd.get('state') or '').lower()
        media = pd.get('media_error_count') or 0
        other = pd.get('other_error_count') or 0
        pred = pd.get('predictive_failure_count') or 0
        smart = (pd.get('smart_alert') or '')
        if stt in ("failed", "offline", "missing") or media > 0 or other > 0 or pred > 0 or smart.lower() == 'yes':
            abnormal.append(pd)

    if not abnormal:
        st.write("- No abnormal PD counters/states detected (all counters are 0 and states are Online/Optimal where available).")
    else:
        for pd in abnormal:
            media = pd.get('media_error_count') or 0
            other = pd.get('other_error_count') or 0
            pred = pd.get('predictive_failure_count') or 0
            smart = pd.get('smart_alert') or 'N/A'
            temp = pd.get('temperature_c')
            temp_str = f"{temp}C" if temp is not None else "N/A"
            st.write(f"- {pd.get('slot')}: State={pd.get('state')}, MediaErr={media}, OtherErr={other}, PredFail={pred}, SMART={smart}, Temp={temp_str}")
