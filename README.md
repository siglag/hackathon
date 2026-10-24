# Local Medical Assistant

A locally hosted workspace that helps a doctor read records, organize patient data, create files, and research medical questions.
Implements `medical-assistant-project-spec.md` (MVP). Patient records, chat, OCR, model inference, and generated files stay on the workstation.
Public literature comes from the hosted PubMed MCP, reached only through a restricted local gateway.

```
Phone (camera) ──HTTPS──┐
Desktop UI ─────────────┼─> API (FastAPI :8600) ──> SQLite + files (~/.local/share/medassist)
                        │        ├─> PaddleOCR PP-OCRv5 + OpenCV (local)
                        │        ├─> Agent adapter ──> Ollama qwen3:4b (local)
                        │        └─> Evidence gateway (:8601, loopback) ──HTTPS──> pubmed.caseyjhand.com/mcp
                        │               (the ONLY process with internet egress)
```

## Quick start (WSL + Ollama on Windows)

```bash
# 1. Backend (Python 3.12; PaddlePaddle has no 3.14 wheels)
cd backend
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[ocr,dev]'

# 2. Setup step: download OCR models (no patient data loaded)
.venv/bin/python -I ../scripts/setup_models.py

# 3. Frontend
cd ../frontend && npm install && npm run build

# 4. Run relay (if needed) + gateway + API
cd .. && scripts/run.sh
# open http://127.0.0.1:8600 and create the doctor account
```

### Ollama on Windows, app in WSL

Windows Ollama listens on Windows-localhost (`127.0.0.1:11434`), which a NAT-mode WSL2 distro cannot reach.
Opening the Windows firewall needs admin rights. `scripts/ollama_wsl_relay.py` solves this without admin rights:

* It listens on WSL's `127.0.0.1:11434` (loopback only; it refuses to bind elsewhere).
* It forwards each request through `curl.exe`, a Windows process started via WSL interop that can reach Windows-localhost.
* Request bodies travel over stdin, never on a command line. Streaming responses pass through unchanged (`curl --raw`).
* Only `/api/*` and `/v1/*` paths are forwarded.

`scripts/run.sh` starts the relay automatically when it detects WSL and nothing answers on `127.0.0.1:11434`.

Alternatives, if you prefer:

* Set `networkingMode=mirrored` in `%UserProfile%\.wslconfig`, then run `wsl --shutdown`. After that, `127.0.0.1:11434` reaches Windows directly.
* Set `MEDASSIST_OLLAMA_URL` to any other private route.

Do not set `OLLAMA_HOST=0.0.0.0` on the Windows side; that would expose Ollama to the LAN (spec §14).

### Phone capture (HTTPS on the clinic LAN)

Mobile browsers allow camera access only over trusted HTTPS.

```bash
scripts/make_local_ca.sh 192.168.1.20 medassist.local    # creates a clinic CA + server cert
HOST=0.0.0.0 TLS_CERT=~/.local/share/medassist/tls/server.crt TLS_KEY=~/.local/share/medassist/tls/server.key \
  MEDASSIST_ALLOWED_ORIGINS=https://192.168.1.20:8600 scripts/run.sh
```

Install `ca.crt` on each phone. In the desktop UI, choose **Scan with phone**, scan the QR code, then confirm the device on the workstation.
On WSL, LAN access additionally requires mirrored networking or a Windows port proxy. Never use a public upload relay.

## What is implemented

| Spec area | Implementation |
|---|---|
| §4 Privacy boundary | Separate gateway process (`app/gateway_service.py`), shared-secret auth, loopback bind. An in-process egress guard in the API (`app/netguard.py`, Python audit hook) blocks non-local connections. No telemetry, CDN or cloud fallbacks. |
| §4 Outbound flow | The model proposes structured concepts. Every concept must resolve against the vocabulary (`app/data/vocabulary.json`, 238 starter terms) plus admin additions. Unrecognized terms stay local. The gateway generates the PubMed syntax. Approval binds to a SHA-256 of the exact normalized request, and edits create a new request. Identifier detection (`app/evidence/phi.py`) runs in both the API and the gateway; the gateway receives case identifiers only as hashes. Fetches accept only PMIDs the approved search returned. |
| §3 MCP | Minimal Streamable-HTTP client (`app/evidence/mcp_client.py`). The tool allowlist is `pubmed_search_articles`, `pubmed_fetch_articles` and `pubmed_fetch_fulltext`; tools whose schemas have changed are disabled. Server-initiated requests (sampling/elicitation) are refused. HTTPS only, redirects off, host allowlist, size caps, timeouts. |
| §5–6 UI | React/TS/Vite with case sidebar, chat, and a resizable artifact panel. Stages, cancel, retry, approval cards, source chips, citation cards, and version history are supported. Previews: virtualized table (sort, search, filter, cell edit → new version), JSON tree/table, image zoom/rotate with OCR boxes, PDF pages, sanitized report HTML/Markdown. |
| §7 Phone | Single-use, short-lived pairing token in the URL fragment (never reaches server logs). The doctor confirms the device on the workstation. The phone session is upload-only and bound to one case, and can be revoked. The capture page has a live camera stream, multiple pages, retake, reorder, crop, rotate, blur/glare warnings, upload progress, and receipts. Metadata is stripped from the working copy; originals are retained only if `RETAIN_CAPTURE_ORIGINALS`. |
| §8 OCR / ingestion | Real file-type sniffing, size/page/decompression/pixel limits, immutable original + SHA-256. OpenCV handles EXIF orientation, perspective correction, deskew, and quality checks. PaddleOCR PP-OCRv5 runs locally; PDF text layers are used where present, otherwise pages are OCR'd. The canonical document JSON keeps spans, lines, and bboxes. Rule-based lab parsing runs first; Qwen may only map *existing* cells (anything not literally present is discarded). Values are never converted (e.g. `5,8` stays unparsed). |
| §8.9 Review | Split image/field view. Each field can be confirmed, corrected, or marked unknown, with version checks. A reviewed dataset can be frozen only once every clinically material field is reviewed. Draft datasets are allowed and carry a conspicuous label. |
| §9–10 Agent | `AgentAdapter` boundary. `LocalToolAgent` is a bounded loop (max 6 steps, 3 tool failures) with only the spec's tools, resolving IDs server-side within the active case. There is no terminal, filesystem, browser, memory, or skills access. The Hermes adapter is a gated stub (see below). |
| §11 Evidence answers | Passages get IDs E1…, and the model may cite only those plus reviewed local records R1…. Unknown IDs are rejected and unsupported claims removed. Lexical support is checked, and weak support is flagged. Links are built from metadata. Retracted papers are excluded. The answer abstains when evidence is insufficient or the provider is unavailable. Literature findings and case interpretation are shown separately. |
| §12–14 Data / security | SQLite + FTS5. The data model follows §12. Authenticated endpoints (including previews, SSE, and downloads), CSRF double-submit, origin checks, case authorization on every object, and idempotency keys for uploads and artifacts. Logs contain route templates and opaque IDs only. Case deletion removes rows, files, caches, FTS entries, and sessions. |
| §6 Files | XLSX (with a Provenance sheet), CSV, JSON, DOCX, PDF, and MD from one report model. Formula-like text is escaped (`'=`), and openpyxl never writes formulas. Workbook formulas without a cached value show as *unavailable*. Macros and external links are never executed. |

## Milestone-0 findings on this workstation (24 Oct 2026)

* **Ollama 0.40.10, `qwen3:4b` digest `359d7dd4…`, Q4_K_M, RTX 3050 6 GB.** Ollama reports this artifact as a thinking-only build with a **262,144-token** context. That differs from the original Qwen3-4B (32K) the spec assumed, so the model window does not block Hermes' 64K minimum. `think:false` does not disable reasoning on this build (the reasoning lands in `content`), so the tool loop uses `think:true` and discards the reasoning. For JSON tasks (classification, topic proposal, extraction mapping, evidence drafting), the model spent its whole 5.9K-token budget reasoning and returned nothing. Those tasks therefore use a raw ChatML prompt with an empty `<think></think>` block (`MEDASSIST_RAW_JSON_NO_THINK=1`).
* `num_ctx=16384` put 21% of the model on the CPU (5.4 GB), so the default is 8192, which fits fully on the GPU. Prompts are bounded to fit it, e.g. at most 8 evidence passages of 450 characters.
* The relay kills `curl.exe` when its client disconnects. Otherwise, timed-out generations kept running in Ollama and later requests queued behind them.
* **Hermes is not integrated yet.** `MEDASSIST_AGENT_BACKEND=hermes` refuses to start. The 64K KV-cache memory on a 6 GB GPU, Hermes tool-calling with this artifact, and a pinned Hermes release are unvalidated (spec §9 gate). The local adapter implements the same session/message/tool-event/cancel contract.
* **Hosted PubMed MCP:** live `initialize` and `tools/list` succeeded (server 2.10.23, protocol 2025-06-18, no auth). The three approved tools were present, with schemas matching the gateway mapping. Only synthetic topics were sent. Quotas, retention terms, and reliability remain unverified.
* **PaddleOCR 3.7 / Paddle 3.3:** oneDNN kernels fail on these models, so `enable_mkldnn=False` is set. A clean synthetic page takes about 10 s on CPU once loaded; the first page includes about 6 s of model load.
* Targets in §15 are **not** benchmarked yet; no accuracy claims are made.

## Tests

```bash
cd backend && .venv/bin/python -m pytest -q
```

The tests cover the outbound policy (vocabulary resolution, identifier detection, hash binding, gateway re-validation), case and phone isolation, CSRF/origin checks, single-use pairing, formula escaping, "model cannot introduce values", unit and decimal preservation, formula cells without a cache, transforms creating versions, and deletion.

## Configuration

All settings are `MEDASSIST_*` environment variables (`backend/app/config.py`), e.g.:

* `DATA_DIR`: keep it outside cloud-synced folders, on an encrypted disk.
* `OLLAMA_URL`, `MODEL`, `NUM_CTX`, `THINK`, `AGENT_MAX_STEPS`.
* `GATEWAY_URL`, `MCP_URL`, `MCP_ALLOWED_HOSTS`.
* `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_SHEET_ROWS`, `MAX_DECOMPRESSED_MB`, `MAX_IMAGE_MEGAPIXELS`.
* `RETAIN_CAPTURE_ORIGINALS`, `RETENTION_DAYS` (set per clinic policy), `SECURE_COOKIES`, `ALLOWED_ORIGINS`.
* `OCR_ENGINE` (`paddle`|`none`), `OCR_LANG`.

## Known limitations / not yet done

* OS- or container-level egress denial for the API, OCR, and model processes is a deployment step. The in-process guard is defense in depth only.
* PP-StructureV3 table reconstruction is not wired in. Tables are rebuilt from line grouping of OCR spans, which suits simple printed lab reports.
* Arabic and mixed-language records are unvalidated. Arabic-Indic digits are flagged for manual entry rather than parsed.
* No backup tooling and no automatic retention expiry yet (`RETENTION_DAYS` is recorded, not enforced).
* "Unusual combination" protection is limited to vocabulary-only concepts, at most 4 concepts, and doctor review.
* The acceptance criteria in spec §16 are release gates. They have not been executed, and this is not a clinical-use release.
