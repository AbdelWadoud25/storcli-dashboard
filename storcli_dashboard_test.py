
import streamlit as st
import os
import zipfile
from io import BytesIO
import re

# ----------------------------
# Friendly titles mapping
# ----------------------------
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

LENOVO_MEDIUM_ERROR_REF = "https://support.lenovo.com/hr/en/solutions/ht504153"

# ----------------------------
# Helpers
# ----------------------------
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
        "fail": "Failed",
        "failed": "Failed",
        "missing": "Missing",
    }
    return mapping.get(v, value.strip())

def size_to_gb(num_str: str, unit: str):
    try:
        num = float(num_str)
    except:
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
            sub = sub[:min(end_positions)]
    return sub

def find_first(patterns, text, flags=re.IGNORECASE):
    if not text:
        return None
    for pat in patterns:
        m = re.search(pat, text, flags)
        if m:
            return m.group(1).strip() if m.groups() else m.group(0).strip()
    return None

def find_all_lines_matching(patterns, text):
    hits = []
    if not text:
        return hits
    for line in text.splitlines():
        for pat in patterns:
            if re.search(pat, line, re.IGNORECASE):
                hits.append(line.strip())
                break
    return hits

def extract_drive_id(line: str):
    """
    Best-effort drive identifier from a line:
    - EID:Slt like 134:2
    - Slot Number: 255
    - PD 09(...)
    - Drive 9 / Drive x
    - slot 9
    """
    if not line:
        return None

    m = re.search(r"\b(\d+:\d+)\b", line)
    if m:
        return m.group(1)

    m = re.search(r"\bSlot Number\s*:\s*(\d+)\b", line, re.IGNORECASE)
    if m:
        return f"Slot {m.group(1)}"

    m = re.search(r"\bPD\s*(\d+)\b", line, re.IGNORECASE)
    if m:
        return f"PD {m.group(1)}"

    m = re.search(r"\bDrive\s+([A-Za-z0-9:_-]+)\b", line, re.IGNORECASE)
    if m:
        return f"Drive {m.group(1)}"

    m = re.search(r"\bslot\s*(\d+)\b", line, re.IGNORECASE)
    if m:
        return f"Slot {m.group(1)}"

    return None

# ----------------------------
# Parsers
# ----------------------------
def parse_controller_info(show_all_text: str) -> dict:
    """Parse controller info but we will DISPLAY only selected fields (per your request)."""
    if not show_all_text:
        return {}
    return {
        "model": find_first([r"Model\s*=\s*(.+)"], show_all_text),
        "status": find_first([r"Controller Status\s*=\s*(Optimal|Degraded|Failed)"], show_all_text),
        "serial_number": find_first([r"Serial Number\s*=\s*(.+)"], show_all_text),
        "driver_name": find_first([r"Driver Name\s*=\s*(.+)"], show_all_text),
        "driver_version": find_first([r"Driver Version\s*=\s*(.+)"], show_all_text),

        # parsed but NOT displayed (you requested removal):
        "pci_slot": find_first([r"PCI Address\s*=\s*(.+)"], show_all_text),
        "firmware": find_first([r"Firmware Version\s*=\s*(.+)"], show_all_text),
        "bios_version": find_first([r"Bios Version\s*=\s*(.+)"], show_all_text),
    }

def parse_cachevault_or_bbu(cv_text: str, bbu_text: str) -> dict:
    """
    Parse CacheVault/BBU including Replacement required Yes/No.
    For cv_show_all absent example: "Cachevault is absent!" => status Missing.
    """
    if cv_text:
        top_status = find_first([r"Status\s*=\s*(\w+)"], cv_text)  # Failure/Success
        absent = re.search(r"Cachevault\s+is\s+absent!", cv_text, re.IGNORECASE)
        replacement_required = find_first([r"Replacement required\s+(Yes|No)"], cv_text)

        if absent:
            status = "Missing"
            # If missing, replacement effectively required even if line absent
            if replacement_required is None:
                replacement_required = "Yes"
        else:
            status = "Failed" if (top_status or "").lower() == "failure" else (top_status or "Unknown")

        return {
            "type": "CacheVault",
            "status": status,
            "replacement_required": replacement_required
        }

    if bbu_text:
        top_status = find_first([r"Status\s*=\s*(.+)"], bbu_text)
        replacement_required = find_first([r"Replacement required\s+(Yes|No)"], bbu_text) or \
                               find_first([r"Battery Replacement.*:\s*(Yes|No)"], bbu_text)

        return {
            "type": "BBU",
            "status": top_status,
            "replacement_required": replacement_required
        }

    return {"type": "None", "status": None, "replacement_required": None}

def parse_vd_list_token_based(text: str) -> list:
    """
    Robust VD LIST parser using tokens.
    Works better than strict regex across variants.
    Expected line example:
    0/0   RAID1 Optl  RW ... 3.637 TB VD_1
    """
    if not text:
        return []

    sec = extract_section(text, ["VD LIST"], end_markers=["DG Drive LIST", "TOPOLOGY", "DG Drive", "Drive LIST", "Total Drive Count"])
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
        # tokens[0]=DG/VD, tokens[1]=TYPE, tokens[2]=State
        # size usually located at -3 (number) and -2 (unit) and -1 (name)
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
        vds.append({
            "dg": int(dg),
            "vd_id": int(vd),
            "raid_level": raid_type,
            "state": state_map(state_raw),
            "name": name,
            "size_gb": size_gb
        })

    return vds

def parse_vds_from_topology_fallback(dall_text: str) -> list:
    """
    Fallback VD extraction from DALL TOPOLOGY RAID rows if VD LIST missing.
    RAID rows look like:
    0 - - - - RAID10 Optl N 1.744 TB ...
    0 0 - - - RAID1  Optl N 893.137 GB ...
    We'll take rows where Type starts with RAID and EID:Slot is '-' (not DRIVE).
    """
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

        # DG Arr Row EID:Slot DID Type State BT Size Unit
        m = re.search(
            r"^(?P<dg>\d+)\s+(?P<arr>\d+|-)\s+(?P<row>\d+|-)\s+"
            r"(?P<eid>-|\d+:\d+)\s+(?P<did>-|\d+)\s+"
            r"(?P<type>RAID\d+)\s+(?P<state>\S+)\s+\S+\s+"
            r"(?P<size>\d+(\.\d+)?)\s+(?P<unit>TB|GB)\b",
            line, re.IGNORECASE
        )
        if not m:
            continue
        # Skip DRIVE rows (they won't match RAID\d+ type anyway)
        if m.group("eid") != "-":
            continue

        dg = int(m.group("dg"))
        arr = m.group("arr")
        raid_type = m.group("type")
        state_raw = m.group("state")
        size_gb = size_to_gb(m.group("size"), m.group("unit"))

        # Create a stable identifier for display
        vd_name = f"DG{dg}-Arr{arr}" if arr != "-" else f"DG{dg}"
        vds.append({
            "dg": dg,
            "vd_id": None,
            "raid_level": raid_type,
            "state": state_map(state_raw),
            "name": vd_name,
            "size_gb": size_gb
        })

    # de-duplicate (sometimes topology repeats)
    uniq = []
    seen = set()
    for vd in vds:
        key = (vd.get("dg"), vd.get("name"), vd.get("raid_level"), vd.get("size_gb"), vd.get("state"))
        if key not in seen:
            seen.add(key)
            uniq.append(vd)
    return uniq

def parse_topology_pds_from_dall(dall_text: str) -> list:
    """
    Parse PDs from DALL TOPOLOGY table using DRIVE rows:
    0 0 1 134:1 1 DRIVE Onln N 893.137 GB ...
    """
    if not dall_text:
        return []
    topo = extract_section(dall_text, ["TOPOLOGY"], end_markers=["VD LIST", "DG Drive LIST", "Total Drive Count"])
    if not topo:
        return []

    pds = []
    for line in topo.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("DG "):
            continue

        m = re.search(
            r"^(?P<dg>\d+)\s+(?P<arr>\d+|-)\s+(?P<row>\d+|-)\s+"
            r"(?P<eid>\d+:\d+|-)\s+(?P<did>\d+|-)\s+"
            r"(?P<type>DRIVE|RAID\d+)\s+(?P<state>\S+)\s+\S+\s+"
            r"(?P<size>\d+(\.\d+)?)\s+(?P<unit>TB|GB)\b",
            line, re.IGNORECASE
        )
        if not m or m.group("type").upper() != "DRIVE":
            continue

        pds.append({
            "slot": m.group("eid"),  # EID:Slot is canonical
            "did": int(m.group("did")) if m.group("did").isdigit() else None,
            "dg": int(m.group("dg")),
            "state": state_map(m.group("state")),
            "size_gb": size_to_gb(m.group("size"), m.group("unit")),
            # merged from SALL
            "media_error_count": None,
            "other_error_count": None,
            "predictive_failure_count": None,
            "smart_alert": None,
            "temperature_c": None,
        })
    return pds

def parse_sall_drive_counters(sall_text: str) -> dict:
    """
    Parse SALL blocks:
    Drive /c0/e134/s2 State :
    Media Error Count = 0
    Other Error Count = 0
    Predictive Failure Count = 0
    S.M.A.R.T alert flagged by drive = No
    Drive Temperature = 21C ...
    """
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
        pred  = find_first([r"Predictive Failure Count\s*=\s*(\d+)"], blk)
        smart = find_first([r"S\.M\.A\.R\.T\s*alert\s*flagged\s*by\s*drive\s*=\s*(Yes|No)"], blk)
        temp  = find_first([r"Drive Temperature\s*=\s*(\d+)\s*C"], blk)

        counters[eid_slot] = {
            "media_error_count": int(media) if media is not None else None,
            "other_error_count": int(other) if other is not None else None,
            "predictive_failure_count": int(pred) if pred is not None else None,
            "smart_alert": smart,
            "temperature_c": int(temp) if temp is not None else None
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

# ----------------------------
# Evidence scan (only real problems)
# ----------------------------
def scan_evidence(blob_text: str) -> dict:
    # Fault events (real problems)
    disabled_fault_lines = find_all_lines_matching([
        r"has\s+been\s+disabled\s+due\s+to\s+a\s+detected\s+fault",
        r"disabled\s+due\s+to\s+a\s+detected\s+fault",
        r"drive\s+has\s+fault",
        r"\bfaulty\b",
    ], blob_text)

    predictive_lines = find_all_lines_matching([
        r"failure\s+predicted\s+on\s+drive",
        r"predictive\s+failure",
        r"pred\s+fail",
    ], blob_text)

    medium_error_lines = find_all_lines_matching([
        r"puncturing\s+bad\s+block\s+on\s+pd",
        r"background\s+initialization\s+detected\s+uncorrectable\s+multiple\s+medium\s+errors",
        r"patrol\s+read\s+found\s+an\s+uncorrectable\s+medium\s+error\s+on\s+pd",
        r"consistency\s+check\s+detected\s+uncorrectable\s+multiple\s+medium\s+errors",
        r"double\s+media\s+errors\s+found",
        r"unexpected\s+sense",
        r"uncorrectable\s+medium\s+error",
    ], blob_text)

    # “Foreign Config Import = Yes” is NOT a problem. Treat as Info/feature only.
    foreign_config_import_lines = find_all_lines_matching([
        r"Foreign\s+Config\s+Import\s*=\s*Yes",
        r"Foreign\s+Config\s+Import\s*:\s*Yes",
    ], blob_text)

    # If you want to detect a REAL foreign-config risk later, you can add different patterns.
    return {
        "disabled_fault_lines": disabled_fault_lines,
        "predictive_lines": predictive_lines,
        "medium_error_lines": medium_error_lines,
        "foreign_config_import_lines": foreign_config_import_lines,  # info only
    }

# ----------------------------
# Rule Engine (Health + Findings + Actions)
# ----------------------------
def compute_overall_health(cache: dict, vd_summary: dict, pd_summary: dict, evidence: dict):
    reasons = []

    # Critical
    if vd_summary.get("offline_vds", 0) > 0:
        reasons.append("One or more Virtual Drives are Offline.")
    if (cache.get("type") in ("CacheVault", "BBU")):
        stt = (cache.get("status") or "").lower()
        if stt in ("missing", "failed", "failure"):
            reasons.append(f"{cache.get('type')} is {cache.get('status')}.")
        if (cache.get("replacement_required") or "").lower() == "yes":
            reasons.append(f"{cache.get('type')} replacement required = Yes.")

    if evidence.get("predictive_lines"):
        reasons.append("Predictive failure event detected in logs.")
    if pd_summary.get("predictive_failure_pds", 0) > 0:
        reasons.append("Predictive Failure Count > 0 detected on one or more drives.")

    # Degraded reasons
    if vd_summary.get("degraded_vds", 0) > 0:
        reasons.append("One or more Virtual Drives are Degraded.")
    if evidence.get("disabled_fault_lines"):
        reasons.append("A drive was disabled due to a detected fault.")
    if evidence.get("medium_error_lines"):
        reasons.append("Medium error patterns detected in logs.")
    if pd_summary.get("media_error_pds", 0) > 0:
        reasons.append("Media Error Count > 0 detected on one or more drives.")

    # Decide final health from reasons (priority)
    critical_triggers = (
        vd_summary.get("offline_vds", 0) > 0
        or evidence.get("predictive_lines")
        or pd_summary.get("predictive_failure_pds", 0) > 0
        or ((cache.get("status") or "").lower() in ("missing", "failed", "failure"))
        or ((cache.get("replacement_required") or "").lower() == "yes")
    )
    degraded_triggers = (
        vd_summary.get("degraded_vds", 0) > 0
        or evidence.get("disabled_fault_lines")
        or evidence.get("medium_error_lines")
        or pd_summary.get("media_error_pds", 0) > 0
    )

    if critical_triggers:
        return "Critical", reasons
    if degraded_triggers:
        return "Degraded", reasons
    return "Healthy", []

def generate_findings(cache: dict, vd_summary: dict, pd_summary: dict, evidence: dict) -> list:
    """
    Findings ONLY when there is a real problem (per your request).
    """
    findings = []

    # CacheVault/BBU problems
    if cache.get("type") in ("CacheVault", "BBU"):
        stt = (cache.get("status") or "").lower()
        if stt in ("missing", "failed", "failure"):
            findings.append({
                "severity": "Critical",
                "component": "Controller",
                "message": f"{cache.get('type')} is {cache.get('status')}.",
                "evidence": "CacheVault/BBU status indicates missing/failed."
            })
        if (cache.get("replacement_required") or "").lower() == "yes":
            findings.append({
                "severity": "Critical",
                "component": "Controller",
                "message": f"{cache.get('type')} replacement required = Yes.",
                "evidence": "Replacement required field = Yes."
            })

    # VD problems
    if vd_summary.get("offline_vds", 0) > 0:
        findings.append({
            "severity": "Critical",
            "component": "VD",
            "message": f"{vd_summary['offline_vds']} Virtual Drive(s) are Offline.",
            "evidence": "VD state detected as Offline."
        })

    if vd_summary.get("degraded_vds", 0) > 0:
        findings.append({
            "severity": "Warning",
            "component": "VD",
            "message": f"{vd_summary['degraded_vds']} Virtual Drive(s) are Degraded.",
            "evidence": "VD state detected as Degraded."
        })

    # PD explicit fault events (always real problems)
    if evidence.get("disabled_fault_lines"):
        findings.append({
            "severity": "Critical",
            "component": "PD",
            "message": "Drive disabled due to a detected fault.",
            "evidence": evidence["disabled_fault_lines"][0]
        })

    if evidence.get("predictive_lines"):
        findings.append({
            "severity": "Critical",
            "component": "PD",
            "message": "Failure predicted / predictive failure event detected.",
            "evidence": evidence["predictive_lines"][0]
        })

    # PD counters (only if > 0)
    if pd_summary.get("predictive_failure_pds", 0) > 0:
        findings.append({
            "severity": "Critical",
            "component": "PD",
            "message": "Predictive Failure Count > 0 detected.",
            "evidence": "SALL counters show Predictive Failure Count > 0."
        })

    if pd_summary.get("media_error_pds", 0) > 0:
        findings.append({
            "severity": "Warning",
            "component": "PD",
            "message": "Media Error Count > 0 detected.",
            "evidence": "SALL counters show Media Error Count > 0."
        })

    # Medium error family (only if detected)
    if evidence.get("medium_error_lines"):
        findings.append({
            "severity": "Warning",
            "component": "PD",
            "message": "Medium error patterns detected.",
            "evidence": evidence["medium_error_lines"][0]
        })

    # Top 3 by severity priority
    priority = {"Critical": 0, "Warning": 1, "Info": 2}
    findings_sorted = sorted(findings, key=lambda x: priority.get(x["severity"], 9))
    return findings_sorted[:3]

def map_actions(findings: list, evidence: dict) -> list:
    """
    Only generate actions that match existing findings (no fixed lines shown by default).
    """
    actions = []

    disabled_ids = {extract_drive_id(x) for x in evidence.get("disabled_fault_lines", []) if extract_drive_id(x)}
    pred_ids = {extract_drive_id(x) for x in evidence.get("predictive_lines", []) if extract_drive_id(x)}
    combined_two_drive_scenario = bool(disabled_ids) and bool(pred_ids) and (disabled_ids != pred_ids)

    for f in findings:
        msg = (f.get("message") or "").lower()
        comp = f.get("component")

        # CacheVault/BBU
        if comp == "Controller" and ("cachevault" in msg or "bbu" in msg or "replacement required" in msg):
            actions.append({
                "priority": "Immediate",
                "action": "Replace the CacheVault/BBU module as required and verify cache returns to Write-Back.",
                "lenovo_sop_ref": None
            })
            continue

        # VD Offline/Degraded
        if comp == "VD" and "offline" in msg:
            actions.append({
                "priority": "Immediate",
                "action": "Identify the impacted VD(s), confirm failed PD(s), replace failed hardware, and validate rebuild/recovery path.",
                "lenovo_sop_ref": None
            })
            continue

        if comp == "VD" and "degraded" in msg:
            actions.append({
                "priority": "High",
                "action": "Confirm the cause of degradation (failed PD/rebuild). Monitor rebuild progress until VD returns Optimal.",
                "lenovo_sop_ref": None
            })
            continue

        # PD Faulty/Disabled scenario
        if comp == "PD" and ("disabled" in msg or "fault" in msg):
            if combined_two_drive_scenario:
                actions.append({
                    "priority": "Immediate",
                    "action": (
                        "Replace the disabled drive first and wait until rebuild completes. "
                        "Then set the predictive-failure drive Offline (if supported) and replace it, "
                        "verifying rebuild starts and VD returns Optimal."
                    ),
                    "lenovo_sop_ref": None
                })
            else:
                actions.append({
                    "priority": "Immediate",
                    "action": "Replace the faulty/disabled drive and verify rebuild starts/continues normally.",
                    "lenovo_sop_ref": None
                })
            continue

        # Predictive failure scenario
        if comp == "PD" and "predictive" in msg:
            actions.append({
                "priority": "High",
                "action": "Plan proactive drive replacement: set the drive Offline (if supported), replace it, and monitor rebuild + VD health.",
                "lenovo_sop_ref": None
            })
            continue

        # Medium error scenario (wording per your request)
        if comp == "PD" and "medium error" in msg:
            actions.append({
                "priority": "High",
                "action": f"Check Lenovo guidance if applicable: {LENOVO_MEDIUM_ERROR_REF}",
                "lenovo_sop_ref": LENOVO_MEDIUM_ERROR_REF
            })
            continue

    return actions[:len(findings)]

# ----------------------------
# STREAMLIT UI (Viewer + Search)
# ----------------------------
st.set_page_config(page_title="StorCLI Log Dashboard (v3)", layout="wide")
st.title("📊 StorCLI Log Dashboard (v3)")
st.markdown("Upload StorCLI `.txt` files or a `.zip` archive. Search logs and view a structured Summary at the end.")

uploaded_zip = st.file_uploader("📦 Upload a ZIP file with StorCLI `.txt` logs", type=["zip"])
uploaded_files = st.file_uploader("📄 Or upload individual `.txt` files", type=["txt"], accept_multiple_files=True)

all_files = []

# Handle ZIP
if uploaded_zip:
    with zipfile.ZipFile(BytesIO(uploaded_zip.read())) as z:
        txt_files = [f for f in z.namelist() if f.lower().endswith(".txt")]
        for filename in txt_files:
            with z.open(filename) as file:
                content = file.read().decode("utf-8", errors="replace")
                all_files.append((filename, content))

# Handle TXT
if uploaded_files:
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="replace")
        all_files.append((file.name, content))

search_term = st.text_input("🔍 Search logs by keyword (e.g., SAS, FRU, medium error, predictive):").strip()

# Index logs by normalized filename for parsing
log_index = {}
for fname, content in all_files:
    log_index[normalize_filename(fname)] = content

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
            unsafe_allow_html=True
        )

    expander_title = f"📁 {title}"
    if search_term and has_hits:
        expander_title = f"📁 {title} — ✅ {hit_count} match(es)"

    with st.expander(expander_title):
        st.code("\n".join(filtered_lines), language="text")

# ----------------------------
# Summary Panel (END)
# ----------------------------
st.markdown("---")
st.subheader("🧾 Summary + Findings + Recommended Actions")

if not all_files:
    st.info("Upload logs to generate the summary.")
else:
    show_all = log_index.get("show_all.txt", "")
    dall = log_index.get("dall_show_all.txt", "")
    vall = log_index.get("vall_show_all.txt", "")
    sall = log_index.get("sall_show_all.txt", "")
    cv = log_index.get("cv_show_all.txt", "")
    bbu = log_index.get("bbu_show_all.txt", "")
    termlog = log_index.get("termlog.txt", "")
    mr_events = log_index.get("mr_events.txt", "") or log_index.get("mr_events0.txt", "")

    controller = parse_controller_info(show_all)
    cache = parse_cachevault_or_bbu(cv, bbu)

    # VD parsing: prefer VD LIST token-based; fallback to TOPOLOGY RAID rows
    vd_src = vall or dall or show_all
    vds = parse_vd_list_token_based(vd_src)
    if not vds and dall:
        vds = parse_vds_from_topology_fallback(dall)

    vd_summary = {
        "total_vds": len(vds),
        "degraded_vds": sum(1 for x in vds if (x.get("state") or "").lower() == "degraded"),
        "offline_vds": sum(1 for x in vds if (x.get("state") or "").lower() == "offline"),
        "vds": vds
    }

    # PD summary: DALL TOPOLOGY + SALL counters
    pds_from_dall = parse_topology_pds_from_dall(dall)
    counter_map = parse_sall_drive_counters(sall)
    pds = merge_pd_counters(pds_from_dall, counter_map)

    pd_summary = {
        "total_pds": len(pds),
        "failed_pds": sum(1 for x in pds if (x.get("state") or "").lower() in ("failed", "offline")),
        "predictive_failure_pds": sum(1 for x in pds if (x.get("predictive_failure_count") or 0) > 0),
        "media_error_pds": sum(1 for x in pds if (x.get("media_error_count") or 0) > 0),
        "other_error_pds": sum(1 for x in pds if (x.get("other_error_count") or 0) > 0),
    }

    # Evidence scan across all logs
    blob = "\n".join([show_all, dall, vall, sall, cv, bbu, termlog, mr_events])
    evidence = scan_evidence(blob)

    overall_health, health_reasons = compute_overall_health(cache, vd_summary, pd_summary, evidence)

    # Overall health banner + reasons
    health_color = {"Healthy": "#2e7d32", "Degraded": "#ef6c00", "Critical": "#c62828"}.get(overall_health, "#1565c0")
    st.markdown(
        f"""
        <div style="padding:10px 14px;border-radius:8px;border-left:6px solid {health_color};background:#f7f7f7;">
          <b>Overall Health:</b> <span style="color:{health_color};font-weight:700;">{overall_health}</span>
        </div>
        """,
        unsafe_allow_html=True
    )
    if health_reasons:
        st.caption("Reason(s): " + " | ".join(sorted(set(health_reasons))))

    # ----------------------------
    # Controller Summary (DISPLAY FILTERED per your request)
    # ----------------------------
    st.markdown("### 🎛️ Controller Summary")
    controller_display = {
        "model": controller.get("model"),
        "status": controller.get("status"),
        "serial_number": controller.get("serial_number"),
        "driver": f"{controller.get('driver_name')} ({controller.get('driver_version')})"
                  if controller.get("driver_name") or controller.get("driver_version") else None,
        "battery_or_cv": {
            "type": cache.get("type"),
            "status": cache.get("status"),
            "replacement_required": cache.get("replacement_required")
        }
    }
    # Remove None values
    controller_display = {k: v for k, v in controller_display.items() if v is not None}
    st.json(controller_display)

    # ----------------------------
    # VD / PD Summary (avoid empty noise; show correct VD counts)
    # ----------------------------
    st.markdown("### 🧩 VD / PD Summary")
    summary_display = {
        "vd_summary": {
            "total_vds": vd_summary["total_vds"],
            "degraded_vds": vd_summary["degraded_vds"],
            "offline_vds": vd_summary["offline_vds"],
        },
        "pd_summary": pd_summary
    }
    st.json(summary_display)

    # Optional: show parsed VD list only if we found any
    if vd_summary["total_vds"] > 0:
        with st.expander("Show parsed VD list"):
            st.json(vd_summary["vds"])

    # Optional: show PD details only if user wants
    with st.expander("Show merged PD details (DALL + SALL)"):
        st.json(pds)

    # ----------------------------
    # Findings + Actions (ONLY if problem exists)
    # ----------------------------
    findings = generate_findings(cache, vd_summary, pd_summary, evidence)
    recommended_actions = map_actions(findings, evidence)

    st.markdown("### 🧠 Findings (Top 3)")
    if findings:
        for i, f in enumerate(findings, start=1):
            st.markdown(f"**{i}. [{f['severity']}] {f['component']}** — {f['message']}")
            st.caption(f"Evidence: {f['evidence']}")
    else:
        st.success("No issues detected (no disabled/predictive/fault/cache replacement/VD offline/degraded/medium-error patterns found).")

    st.markdown("### ✅ Recommended Actions")
    if recommended_actions:
        for a in recommended_actions:
            st.markdown(f"**[{a['priority']}]** {a['action']}")
    else:
        st.info("No actions required.")

    # ----------------------------
    # Notes (Info-only signals, not problems)
    # ----------------------------
    if evidence.get("foreign_config_import_lines"):
        st.markdown("### ℹ️ Notes (Detected Features)")
        st.write("Foreign Config Import = Yes (feature/setting detected; not treated as a fault).")
        st.caption(f"Evidence: {evidence['foreign_config_import_lines'][0]}")

    with st.expander("🔧 Debug: Evidence signals"):
        st.json(evidence)
