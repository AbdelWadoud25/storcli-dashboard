import streamlit as st
import pandas as pd
import os
import zipfile
from io import BytesIO
import re

# Mapping of filenames to meaningful titles
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

# Streamlit layout
st.set_page_config(page_title="StorCLI Log Dashboard", layout="wide")
st.title("📊 StorCLI Log Dashboard")
st.markdown("Upload your StorCLI `.txt` files or a `.zip` archive to view them with meaningful titles.")

# Upload options
uploaded_zip = st.file_uploader("📦 Upload a ZIP file with StorCLI `.txt` logs", type=["zip"])
uploaded_files = st.file_uploader("📄 Or upload individual `.txt` files", type=["txt"], accept_multiple_files=True)

# Unified list for parsed files
all_files = []

# --- Handle ZIP file ---
if uploaded_zip:
    try:
        with zipfile.ZipFile(BytesIO(uploaded_zip.read())) as z:
            txt_files = [f for f in z.namelist() if f.lower().endswith(".txt")]
            if not txt_files:
                st.warning("⚠️ No `.txt` files found in the uploaded ZIP archive.")
            for filename in txt_files:
                with z.open(filename) as file:
                    content = file.read().decode("utf-8", errors="ignore")
                    all_files.append((filename, content))
        st.success(f"✅ Loaded {len(txt_files)} text files from the ZIP archive.")
    except zipfile.BadZipFile:
        st.error("❌ Invalid or corrupted ZIP file uploaded.")

# --- Handle individual TXT files ---
if uploaded_files:
    for file in uploaded_files:
        content = file.read().decode("utf-8", errors="ignore")
        all_files.append((file.name, content))

# --- Search bar ---
search_input = st.text_input(
    "🔍 Search logs by keywords (comma-separated, e.g., error, failed, rebuild, bad block):"
).strip()
search_terms = [t.strip() for t in search_input.split(",") if t.strip()]

# --- Display section ---
if all_files:
    st.subheader("📂 Available Log Files")
    for filename, content in all_files:
        display_title = file_titles.get(os.path.basename(filename), filename)

        # Apply search filter if terms exist
        if search_terms:
            if not any(re.search(term, content, re.IGNORECASE) for term in search_terms):
                continue  # Skip non-matching files

        # Use an expander (like a collapsible folder)
        with st.expander(f"📁 {display_title}"):
            if search_terms:
                highlighted = content
                for term in search_terms:
                    highlighted = re.sub(
                        f"({re.escape(term)})",
                        r"**\1**",
                        highlighted,
                        flags=re.IGNORECASE
                    )
                st.code(highlighted, language="text")
            else:
                st.code(content, language="text")
else:
    st.info("📤 Please upload a `.zip` archive or `.txt` files to begin.")
