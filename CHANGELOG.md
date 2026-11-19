# Changelog

## Unreleased
- NEW: `get_client` tool (read-only): full client registry detail by ID — address, PEC, phone, SDI code. `list_clients` only returns name, VAT, tax code and email. Reuses the existing cached `get_client_by_id` helper.

## v2.0.0
- FIX: `list_cost_centers` and `revenue_center` / `cost_center` validation now correctly query the right FIC API endpoint. FIC exposes cost centers and revenue centers as **two separate registries** (`/info/cost_centers` and `/info/revenue_centers`); the previous implementation only queried `/info/cost_centers`, which silently broke `revenue_center` validation on every issued document tool when the account had only revenue centers configured (the common case). Fix: two internal cached fetchers (`fetch_cost_centers`, `fetch_revenue_centers`), the `list_cost_centers` MCP tool returns their deduplicated union (matching the FIC UI "Analisi centri c/r" view), and validation on document mutations is type-specific (issued documents validate `revenue_center` against revenue_centers; received documents validate `cost_center` against cost_centers).
- NEW: MCPB bundle for Claude Desktop one-click installation. Distributable artifact `dist/fattureincloud.mcpb` produced by `scripts/build.sh`. Same 23 tools as v1.9.0, no behavioral changes for users.
- NEW: `manifest.json` (manifest_version 0.3) declaring the server entry point, runtime deps, user_config (API token + company ID + sender email), and tool annotations.
- NEW: tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on all 23 tools, both in `manifest.json` and on the `Tool()` declarations in `server.py`. Helps MCP clients reason about safe-to-replay vs side-effecting calls.
- NEW: `SECURITY.md` (vulnerability disclosure policy).
- NEW: `docs/PRIVACY.md` (Privacy Policy, mirrored at https://media-form.it/privacy-policy.html and referenced from `manifest.json`).
- NEW: `scripts/build.sh` and `scripts/validate.sh` to produce and validate the bundle.
- NEW: `.mcpbignore` to keep the bundle minimal (no tests, no dev artifacts).
- CHANGE: README extended with MCPB installation path, Privacy & Data handling, Trademark disclaimer, and Contributing sections.
- NOTE: v2.0.0 does not introduce or modify any tool. The version bump reflects the new packaging surface (MCPB) targeted at the Anthropic Software Directory.

## v1.9.0
- NEW: `list_cost_centers` — lista i centri di costo/ricavo configurati in FattureInCloud
- NEW: `get_received_document` — dettaglio fattura passiva per ID
- NEW: `create_received_document` — crea documento passivo (expense / credit_note) con fornitore, importi, categoria, descrizione, opzionale `cost_center`
- NEW: parametro opzionale `revenue_center` su `create_invoice`, `create_credit_note`, `create_proforma`, `convert_proforma_to_invoice`, `update_document`, `duplicate_invoice` (validato contro `list_cost_centers`; convert e duplicate ereditano dal documento sorgente se non passato)
- NEW: parametro opzionale `cost_center` su `create_received_document`
- NEW: cache locale file-based per fetch di anagrafica clienti e centri di costo (key per `company_id`, TTL 24h). Configurabile via `FIC_CACHE_DIR` (default `~/.fattureincloud-mcp/cache/`) e disattivabile via `FIC_CACHE_DISABLED=1`. Riduce le chiamate API ridondanti durante il flusso di creazione fatture (la stessa anagrafica veniva fetchata due volte, ora una sola)
- CHANGE: `get_invoice`, `list_invoices`, `list_received_documents` espongono `revenue_center`/`cost_center` nei risultati quando presenti
- KNOWN ISSUE: la duplicazione fatture può fallire per configurazioni cliente specifiche (workaround: duplicare manualmente dal pannello web FIC). Tracciato in `docs/KNOWN_ISSUES.md`
- NOTE: il modulo `cache.py` è collocato a top level; sarà spostato in `server/cache.py` durante un futuro refactor strutturale del modulo `server`

## v1.8.0
- NEW: `convert_proforma_to_invoice` — converte proforma in fattura elettronica (elimina proforma di default, `keep_proforma=True` per mantenerla)
- NEW: `get_pdf_url` — restituisce URL PDF e link web del documento
- NEW: `create_client` — crea nuovo cliente in anagrafica
- NEW: `update_client` — aggiorna dati cliente esistente
- FIX: `get_situation` — ora sottrae le NDC dal fatturato (fatturato_netto = fatture - NDC) e supporta filtro per cliente
- REMOVED: `mark_payment_paid` / `mark_payment_unpaid` / `list_payment_accounts` — l'API FIC richiede un conto di saldo obbligatorio non recuperabile in modo affidabile via SDK. La marcatura pagamenti va eseguita direttamente dal pannello FIC.

## v1.6.4
- FIX: `create_credit_note` — prezzi e payment positivi, FIC inverte internamente per type=credit_note
- FIX: `update_document` — stessa logica per NDC

## v1.6.3
- FIX: tentativo NDC senza payments_list (non funzionava)

## v1.6.2
- FIX: tentativo NDC con payment negativo (non funzionava)

## v1.6.1
- FIX: tentativo abs() su payment per NDC (non funzionava)

## v1.6.0
- NEW: `update_document` — modifica parziale di qualsiasi documento bozza (fattura, NDC, proforma): data, oggetto, righe, giorni pagamento. Carica l'originale e applica solo i campi passati.

## v1.5.0
- NEW: `create_credit_note` — crea nota di credito; importi positivi in input, negativi automaticamente; `source_invoice_id` opzionale
- NEW: `create_proforma` — crea proforma, non inviabile allo SDI
- CHANGE: `list_invoices` accetta parametro `type`: `invoice` (default), `credit_note`, `proforma`

## v1.4.0
- NEW: `list_invoices` accetta parametro `type`

## v1.3.0
- FIX: `create_invoice` include `ei_code` dall'anagrafica
- FIX: `duplicate_invoice` aggiorna `ei_code`
- NEW: `check_numeration`

## v1.2.0
- NEW: `delete_invoice`
- NEW: `payment_days` in `duplicate_invoice`

## v1.1.0
- Release iniziale
