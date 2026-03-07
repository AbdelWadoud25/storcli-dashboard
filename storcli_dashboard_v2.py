
import streamlit as st
import pandas as pd
import os
import zipfile
from io import BytesIO
import re

# Updated mapping of filenames to meaningful titles
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
    "termlog.txt": "RAID Controller Ops and Errors Detection"
}

st.set_page_config(page_title="StorCLI Log Dashboard", layout="wide")
st.title("📊 StorCLI Log Dashboard")
st.markdown("Upload your StorCLI `.txt` files or a `.zip` archive to view them with meaningful titles.")

# Upload options
uploaded_zip = st.file_uploader("📦 Upload a ZIP file with StorCLI `.txt` logs", type=["zip"])
uploaded_files = st.file_uploader("📄 Or upload individual `.txt` files", type=["txt"], accept_multiple_files=True)

# Unified list to hold all parsed files
all_files = []

# Handle ZIP file
if uploaded_zip:
    with zipfile.ZipFile(BytesIO(uploaded_zip.read())) as z:
        txt_files = [f for f in z.namelist() if f.endswith(".txt")]
        for filename in txt_files:
            with z.open(filename) as file:
                content = file.read().decode("utf-8", errors="replace")
                all_files.append((filename, content))

# Handle individual TXT files
if uploaded_files:
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="replace")
        all_files.append((file.name, content))

# 🔍 Search bar
search_term = st.text_input("🔍 Search logs by keyword (e.g., SAS, FRU, bad block, medium error):").strip()

# ✅ Enhancement 1: normalize filename so mapping works with prefixes like c0_, c1-, c2...
def normalize_filename_for_title(filename: str) -> str:
    base = os.path.basename(filename)
    # Remove leading controller prefix like: c0_, c1-, c2 (optional underscore/dash)
    base = re.sub(r"^c\d+[_-]?", "", base, flags=re.IGNORECASE)
    return base

# Display each file with meaningful title
for filename, content in all_files:
    normalized_name = normalize_filename_for_title(filename)
    title = file_titles.get(normalized_name, f"Unknown File: {filename}")

    # Filter lines based on search (original logic preserved)
    lines = content.splitlines()
    filtered_lines = [line for line in lines if search_term.lower() in line.lower()] if search_term else lines

    # Match detection
    has_hits = bool(search_term) and len(filtered_lines) > 0
    hit_count = len(filtered_lines) if search_term else 0

    # ✅ NEW (Option A): Hide non-matching expanders completely during search
    if search_term and not has_hits:
        continue

    # ✅ Green banner ABOVE matching section (NO count in banner)
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

    # ✅ Expander title includes match count (only place where count appears)
    expander_title = f"📁 {title}"
    if search_term and has_hits:
        expander_title = f"📁 {title} — ✅ {hit_count} match(es)"

    # Show file content
    with st.expander(expander_title):
        st.code("\n".join(filtered_lines), language="text")
