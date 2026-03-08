1) StorCLI Log Dashboard (v5)

Streamlit application for rapid triage of Broadcom/Avago/Lenovo **MegaRAID (StorCLI)** logs.  
Upload `.zip`/nested `.zip` bundles or individual `.txt` files, search across logs, and get a **clean, per‑controller summary** of:

- Controller model/firmware/driver/status  
- Virtual Drives (VD): total / degraded / offline  
- Physical Drives (PD): failures, predictive alerts, media/other error counters, temperature, SMART flag  
- CacheVault / BBU presence and status (including “absent” cases)  

> Designed to reduce noise in real‑world field logs (inconsistent file names, nested archives, missing sections) and produce a concise, reliable summary for tickets.

---

## Key Features
- **Multi‑file, multi‑controller ingestion**
  - Upload a single **ZIP** file (supports **nested ZIPs**)
  - Or upload multiple `.txt` files directly
  - Optional **RAR** support when enabled via environment variable
- **Smart filename normalization**
  - Handles controller prefixes (`c0_`, `c1-`), CamelCase `*ShowAll.txt` variants, `mr_events0` vs `mr_eventso`, `termlog` aliases, etc.
- **Per‑controller summaries**
  - Controller: model, firmware, driver, status
  - VDs: total / degraded / offline (preserves RAID semantics like **UGood/UBad**)
  - PDs: total, Failed/Offline/Missing, counters > 0 (Media/Other/Predictive), SMART=Yes, temperatures
- **Built‑in search**
  - Search a keyword across all uploaded logs and view matching files with highlights
- **Guardrails**
  - Maximum archive size (`STORCLI_MAX_ZIP_MB`) and bounded nested depth (`STORCLI_MAX_NESTED_DEPTH`)
- **Optional export**
  - **Elasticsearch** JSON export (off by default; enabled via env vars)

---

## What the app parses

- `show_all.txt`: controller model/status/driver/firmware  
- `vall_show_all.txt` (VD LIST preferred): **Virtual Drives**  
- `dall_show_all.txt` (TOPOLOGY): **VD fallback** + **PD topology**  
- `sall_show_all.txt`: per‑drive counters (Media/Other/Predictive/SMART/Temperature)  
- `bbu_show_all.txt` / `cv_show_all.txt`: **CacheVault/BBU** presence & status  
- `mr_events*.txt`, `termlog.txt`: included in the searchable viewer

> If `VALL` is missing, VDs are derived from `DALL` TOPOLOGY.  
> If `DALL` and/or `SALL` are absent, PD detail will be shown as **N/A** to avoid misleading zeros.

---

## Quick Start

### 1) Create & activate a virtual environment
```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Windows CMD
.venv\Scripts\activate.bat
# macOS/Linux
source .venv/bin/activate

2) Install dependencies
pip install -r requirements.txt

 3) Run the app
streamlit run storcli_dashboard.py
Open the local URL (typically http://localhost:8501).


## Live Demo
You can access the hosted version of the application here:
🔗 **https://storcli-dashboard-nzleqced6aqil3pjemvjrw.streamlit.app/**
> Note: The hosted version auto‑updates whenever new changes are pushed to the repository.


## Configuration via Environment Variables
Variable: STORCLI_TEXT_ENCODING - Default: utf-8 - Purpose: Fallback decoding for uploaded .txt
Variable: STORCLI_MAX_ZIP_MB - Default: 200 - Purpose: Maximum uploaded archive size (MB)
Variable: STORCLI_MAX_NESTED_DEPTH - Default: 3 - Purpose: Max recursion for nested ZIPs
Variable: STORCLI_ENABLE_RAR - Default:  0 - Purpose: 1/true/yes to enable RAR (requires rarfile + system unrar/bsdtar)
Variable: STORCLI_ES_URL - Default: (empty) - Purpose: If set (non‑empty), shows a button to export JSON summaries
Variable: STORCLI_ES_API_KEY - Default: (empty) - Purpose: Optional API key; sent as Authorization: ApiKey <key>
Variable: STORCLI_TIMEOUT_SEC - Default: 25 - Purpose: Timeout for the export HTTP request (seconds)
The code trims whitespace: a value with only spaces is treated as empty.


## PowerShell examples (Windows)
Set for the current PowerShell session:
$env:STORCLI_ENABLE_RAR = "1"            # optional
$env:STORCLI_ES_URL = "https://your-es-endpoint.example.com/index"
$env:STORCLI_ES_API_KEY = "YOUR_API_KEY" # optional
streamlit run .\storcli_dashboard.py

## Elasticsearch Export (optional)
When STORCLI_ES_URL is set, the app shows a button:
Send summary to Elasticsearch (experimental)
The POST body looks like:
{
  "summaries": [
    {
      "controller": "C0",
      "controller_info": { "...": "..." },
      "cache": { "type": "BBU", "status": "Optimal", "installed": true },
      "vd_summary": { "total_vds": 2, "degraded_vds": 0, "offline_vds": 0 },
      "pd_summary": { "total_pds": 8, "failed_pds": 0, "predictive_failure_pds": 0, "media_error_pds": 0, "other_error_pds": 0 }
    }
  ]
}
Headers:

Content-Type: application/json
Authorization: ApiKey <STORCLI_ES_API_KEY> (only if provided)
Note: Only the summaries are sent, not the raw logs.


##  Troubleshooting
- **ZIP uploads show no files**  
  Check size vs `STORCLI_MAX_ZIP_MB`. Confirm the archive contains `.txt` (some vendor tools pack other formats). Nested ZIPs are supported up to `STORCLI_MAX_NESTED_DEPTH`.

 **RAR doesn’t work**  
  Install:
  ```bash
  pip install rarfile

## Project Structure
storcli-dashboard/
├─ storcli_dashboard.py    # Streamlit app (entry point)
├─ requirements.txt
└─ README.md

---



