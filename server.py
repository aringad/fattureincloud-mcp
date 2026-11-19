#!/usr/bin/env python3
"""Fatture in Cloud MCP Server - v2.0.0

MCP Server per integrare Fatture in Cloud con Claude AI.
Permette di gestire fatture elettroniche italiane tramite conversazione.

Author: Mediaform s.c.r.l. (https://media-form.it)
License: MIT
"""

import json
import os
import traceback
from datetime import datetime, timedelta

import fattureincloud_python_sdk as fic
from fattureincloud_python_sdk.api.issued_documents_api import IssuedDocumentsApi
from fattureincloud_python_sdk.api.issued_e_invoices_api import IssuedEInvoicesApi
from fattureincloud_python_sdk.api.received_documents_api import ReceivedDocumentsApi
from fattureincloud_python_sdk.api.clients_api import ClientsApi
from fattureincloud_python_sdk.api.companies_api import CompaniesApi
from fattureincloud_python_sdk.api.info_api import InfoApi

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ToolAnnotations

import cache


def _ann(read_only=False, destructive=False, idempotent=False, open_world=True):
    """Shorthand for MCP tool annotations. openWorld defaults to True since
    every tool talks to the FattureInCloud API."""
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )

ACCESS_TOKEN = os.getenv("FIC_ACCESS_TOKEN", "")
COMPANY_ID = int(os.getenv("FIC_COMPANY_ID", "0"))
SENDER_EMAIL = os.getenv("FIC_SENDER_EMAIL", "")

configuration = fic.Configuration()
configuration.access_token = ACCESS_TOKEN
api_client = fic.ApiClient(configuration)

issued_api = IssuedDocumentsApi(api_client)
einvoice_api = IssuedEInvoicesApi(api_client)
received_api = ReceivedDocumentsApi(api_client)
clients_api = ClientsApi(api_client)
companies_api = CompaniesApi(api_client)
info_api = InfoApi(api_client)

app = Server("fattureincloud")


def get_total_from_doc(d):
    payments = d.get('payments_list', [])
    if payments:
        return sum(p.get('amount', 0) for p in payments)
    items = d.get('items_list', [])
    return sum((i.get('qty', 0) * i.get('gross_price', 0)) for i in items)


def get_client_by_id(client_id, *, company_id=None):
    if company_id is None:
        company_id = COMPANY_ID
    resource = f"client_{client_id}"
    hit = cache.get(resource, company_id, ttl=timedelta(hours=24))
    if hit is not None:
        return hit
    try:
        response = clients_api.get_client(company_id=company_id, client_id=client_id)
        data = response.data.to_dict()
        cache.put(resource, company_id, data)
        return data
    except:
        return None


def get_ei_code_for_client(client_id, *, company_id=None):
    if company_id is None:
        company_id = COMPANY_ID
    try:
        client = get_client_by_id(client_id, company_id=company_id)
        if client:
            ei_code = (client.get('ei_code') or '').strip()
            if ei_code:
                return ei_code
            pec = (client.get('certified_email') or '').strip()
            if pec:
                return '0000000'
        return '0000000'
    except:
        return '0000000'


@cache.cached("cost_centers", ttl=timedelta(hours=24))
def fetch_cost_centers(*, company_id):
    """Cost centers (FIC `/info/cost_centers`). Used to validate `cost_center`
    on received documents only."""
    try:
        response = info_api.list_cost_centers(company_id=company_id)
        return list(response.data or [])
    except Exception:
        return []


@cache.cached("revenue_centers", ttl=timedelta(hours=24))
def fetch_revenue_centers(*, company_id):
    """Revenue centers (FIC `/info/revenue_centers`). Used to validate
    `revenue_center` on issued documents only.

    FIC keeps cost and revenue centers as two separate registries; the
    `list_cost_centers` MCP tool returns their union (matching the
    'Analisi centri c/r' view in the FIC UI), but mutation validation
    has to use the type-specific list to match how the FIC API itself
    accepts/rejects values."""
    try:
        response = info_api.list_revenue_centers(company_id=company_id)
        return list(response.data or [])
    except Exception:
        return []


def build_entity_from_client(client_id, client_data=None):
    if not client_data:
        client_data = get_client_by_id(client_id)
    if not client_data:
        return None
    ei_code = get_ei_code_for_client(client_id)
    entity = {
        "id": client_id,
        "name": client_data.get("name", ""),
        "vat_number": client_data.get("vat_number", ""),
        "tax_code": client_data.get("tax_code", ""),
        "address_street": client_data.get("address_street", ""),
        "address_city": client_data.get("address_city", ""),
        "address_postal_code": client_data.get("address_postal_code", ""),
        "address_province": client_data.get("address_province", ""),
        "country": client_data.get("country", "Italia"),
        "ei_code": ei_code,
    }
    pec = (client_data.get("certified_email") or "").strip()
    if pec:
        entity["certified_email"] = pec
    return entity


def build_items_list(items_data, negate=False):
    items_list = []
    for item in items_data:
        vat_rate = item.get("vat_rate", 22)
        net_price = item["net_price"]
        if negate:
            net_price = -abs(net_price)
        items_list.append({
            "name": item["name"],
            "description": item.get("description", ""),
            "qty": item["qty"],
            "net_price": net_price,
            "vat": {"id": 0, "value": vat_rate}
        })
    return items_list


def build_issued_document(doc_type, client_id, items_data, date_str, payment_days,
                          visible_subject, negate_prices=False, source_invoice_id=None,
                          revenue_center=None):
    client_data = get_client_by_id(client_id)
    if not client_data:
        return None, f"Cliente con ID {client_id} non trovato"

    if revenue_center:
        known = fetch_revenue_centers(company_id=COMPANY_ID)
        if revenue_center not in known:
            return None, (
                f"revenue_center '{revenue_center}' non esiste. "
                f"Disponibili: {known}. Crearli da web FIC (Impostazioni → Centri di costo/ricavo)."
            )

    entity = build_entity_from_client(client_id, client_data)
    items_list = build_items_list(items_data, negate=False)

    invoice_date = datetime.strptime(date_str, "%Y-%m-%d")
    due_date = invoice_date + timedelta(days=payment_days)
    total_abs = sum(
        abs(i["qty"] * i["net_price"]) * (1 + i["vat"]["value"] / 100)
        for i in items_list
    )
    result_total = -total_abs if negate_prices else total_abs

    body_data = {
        "type": doc_type,
        "entity": entity,
        "date": date_str,
        "visible_subject": visible_subject,
        "items_list": items_list,
        "payments_list": [{
            "amount": round(total_abs, 2),
            "due_date": due_date.strftime("%Y-%m-%d"),
            "status": "not_paid",
            "payment_terms": {"days": payment_days, "type": "standard"}
        }]
    }

    if revenue_center:
        body_data["rc_center"] = revenue_center
    if doc_type in ("invoice", "credit_note"):
        body_data["e_invoice"] = True
        body_data["ei_data"] = {"payment_method": "MP05"}
    if source_invoice_id:
        body_data["original_document"] = {"id": source_invoice_id}

    response = issued_api.create_issued_document(
        company_id=COMPANY_ID,
        create_issued_document_request={"data": body_data}
    )
    d = response.data.to_dict()

    result = {
        "success": True,
        "id": d.get("id"),
        "number": d.get("number"),
        "date": str(d.get("date", "")),
        "due_date": due_date.strftime("%Y-%m-%d"),
        "client": client_data.get("name"),
        "ei_code": entity.get("ei_code", "N/A"),
        "total": round(result_total, 2),
        "type": doc_type,
        "status": "bozza",
    }
    if revenue_center:
        result["revenue_center"] = revenue_center
    if source_invoice_id:
        result["linked_to_invoice"] = source_invoice_id
    return result, None


@app.list_tools()
async def list_tools():
    item_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Nome prodotto/servizio"},
            "description": {"type": "string", "description": "Descrizione estesa"},
            "qty": {"type": "number", "description": "Quantità"},
            "net_price": {"type": "number", "description": "Prezzo netto unitario (sempre positivo)"},
            "vat_rate": {"type": "number", "description": "Aliquota IVA (es. 22)"}
        },
        "required": ["name", "qty", "net_price"]
    }

    return [
        Tool(
            name="list_invoices",
            description="Lista documenti emessi. Parametri: year (int), month (int opzionale), query (str opzionale), type (str opzionale: invoice, credit_note, proforma — default: invoice)",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno (es. 2024)"},
                    "month": {"type": "integer", "description": "Mese 1-12 (opzionale)"},
                    "query": {"type": "string", "description": "Filtro testuale (opzionale)"},
                    "type": {"type": "string", "description": "Tipo documento: invoice (default), credit_note, proforma"}
                },
                "required": ["year"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_invoice",
            description="Dettaglio documento per ID (fattura, NDC, proforma)",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_pdf_url",
            description="Restituisce URL PDF e link web di un documento (fattura, NDC, proforma)",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="list_clients",
            description="Lista clienti",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filtro nome/ragione sociale (opzionale)"}
                }
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_client",
            description="Dettaglio anagrafica di un cliente: indirizzo, PEC, telefono, codice SDI",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"}
                },
                "required": ["client_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_company_info",
            description="Info azienda collegata",
            inputSchema={"type": "object", "properties": {}},
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="create_client",
            description="Crea nuovo cliente in anagrafica",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nome/Ragione sociale"},
                    "vat_number": {"type": "string", "description": "Partita IVA (opzionale)"},
                    "tax_code": {"type": "string", "description": "Codice fiscale (opzionale)"},
                    "ei_code": {"type": "string", "description": "Codice destinatario SDI (opzionale)"},
                    "certified_email": {"type": "string", "description": "PEC (opzionale)"},
                    "email": {"type": "string", "description": "Email ordinaria (opzionale)"},
                    "address_street": {"type": "string", "description": "Indirizzo (opzionale)"},
                    "address_city": {"type": "string", "description": "Città (opzionale)"},
                    "address_postal_code": {"type": "string", "description": "CAP (opzionale)"},
                    "address_province": {"type": "string", "description": "Provincia (opzionale)"},
                    "country": {"type": "string", "description": "Paese (default: Italia)"},
                    "phone": {"type": "string", "description": "Telefono (opzionale)"}
                },
                "required": ["name"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="update_client",
            description="Aggiorna dati cliente esistente. Passa solo i campi da modificare.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "name": {"type": "string", "description": "Nome/Ragione sociale (opzionale)"},
                    "vat_number": {"type": "string", "description": "Partita IVA (opzionale)"},
                    "tax_code": {"type": "string", "description": "Codice fiscale (opzionale)"},
                    "ei_code": {"type": "string", "description": "Codice destinatario SDI (opzionale)"},
                    "certified_email": {"type": "string", "description": "PEC (opzionale)"},
                    "email": {"type": "string", "description": "Email ordinaria (opzionale)"},
                    "address_street": {"type": "string", "description": "Indirizzo (opzionale)"},
                    "address_city": {"type": "string", "description": "Città (opzionale)"},
                    "address_postal_code": {"type": "string", "description": "CAP (opzionale)"},
                    "address_province": {"type": "string", "description": "Provincia (opzionale)"},
                    "phone": {"type": "string", "description": "Telefono (opzionale)"}
                },
                "required": ["client_id"]
            },
            annotations=_ann(idempotent=True),
        ),
        Tool(
            name="create_invoice",
            description="Crea nuova fattura (bozza). IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {"type": "array", "items": item_schema},
                    "date": {"type": "string", "description": "Data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["client_id", "items"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="create_credit_note",
            description="Crea nota di credito (bozza). Importi POSITIVI in input, resi negativi automaticamente. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {"type": "array", "items": item_schema},
                    "date": {"type": "string", "description": "Data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile"},
                    "source_invoice_id": {"type": "integer", "description": "ID fattura originale da stornare (opzionale)"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["client_id", "items"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="create_proforma",
            description="Crea proforma (bozza). Non inviabile allo SDI. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {"type": "array", "items": item_schema},
                    "date": {"type": "string", "description": "Data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["client_id", "items"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="convert_proforma_to_invoice",
            description="Converte una proforma in fattura elettronica (bozza). Di default elimina la proforma originale. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID proforma da convertire"},
                    "date": {"type": "string", "description": "Data fattura YYYY-MM-DD (default: data proforma)"},
                    "keep_proforma": {"type": "boolean", "description": "Mantieni la proforma originale (default: false)"},
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, eredita da proforma se non passato)"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(destructive=True),
        ),
        Tool(
            name="update_document",
            description="Modifica parziale di un documento BOZZA (fattura, NDC, proforma). Passa solo i campi da aggiornare. Funziona solo su documenti non ancora inviati allo SDI. IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento da modificare"},
                    "date": {"type": "string", "description": "Nuova data YYYY-MM-DD (opzionale)"},
                    "visible_subject": {"type": "string", "description": "Nuovo oggetto visibile (opzionale)"},
                    "payment_days": {"type": "integer", "description": "Nuovi giorni pagamento (opzionale)"},
                    "items": {
                        "type": "array",
                        "items": item_schema,
                        "description": "Nuove righe documento (opzionale). Per NDC, importi sempre positivi."
                    },
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, mantiene quello esistente se non passato)"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(idempotent=True),
        ),
        Tool(
            name="duplicate_invoice",
            description="Duplica una fattura esistente con nuova data (crea bozza). IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_document_id": {"type": "integer", "description": "ID fattura da duplicare"},
                    "new_date": {"type": "string", "description": "Nuova data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "description": "Giorni pagamento (default: eredita da originale)"},
                    "description_replace": {
                        "type": "object",
                        "description": "Sostituzioni testo nella descrizione (es. 2025->2026)",
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"}
                        }
                    },
                    "revenue_center": {"type": "string", "description": "Centro di ricavo (opzionale, eredita dalla fattura sorgente se non passato)"}
                },
                "required": ["source_document_id"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="delete_invoice",
            description="Elimina un documento BOZZA (fattura, NDC, proforma). ATTENZIONE: Azione irreversibile! Chiedere SEMPRE conferma esplicita all'utente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento da eliminare"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(destructive=True, idempotent=True),
        ),
        Tool(
            name="send_to_sdi",
            description="Invia fattura/NDC allo SDI. ATTENZIONE: Azione irreversibile! Chiedere SEMPRE conferma esplicita all'utente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento da inviare"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="get_invoice_status",
            description="Controlla stato e-invoice/SDI di un documento",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="send_email",
            description="Invia copia cortesia via email al cliente. Requires FIC_SENDER_EMAIL to be configured in extension settings. IMPORTANTE: Chiedere conferma prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento"},
                    "recipient_email": {"type": "string", "description": "Email destinatario (opzionale)"},
                    "subject": {"type": "string", "description": "Oggetto email (opzionale)"},
                    "body": {"type": "string", "description": "Corpo email (opzionale)"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(),
        ),
        Tool(
            name="list_received_documents",
            description="Lista fatture PASSIVE (ricevute dai fornitori). Parametri: year, month (opzionale), type (opzionale: expense, credit_note)",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno"},
                    "month": {"type": "integer", "description": "Mese 1-12 (opzionale)"},
                    "type": {"type": "string", "description": "Tipo: expense, credit_note (default: expense)"},
                    "query": {"type": "string", "description": "Filtro testuale (opzionale)"}
                },
                "required": ["year"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_situation",
            description="Dashboard anno: fatturato netto (fatture - NDC), incassato, da incassare, costi, margine. Supporta filtro per cliente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno (default: corrente)"},
                    "client_name": {"type": "string", "description": "Filtro per nome cliente (opzionale, ricerca parziale)"}
                }
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="check_numeration",
            description="Verifica continuità numerica delle fatture emesse per un dato anno.",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno da verificare (es. 2025)"}
                },
                "required": ["year"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="list_cost_centers",
            description="Lista combinata di centri di costo e ricavo configurati in FattureInCloud. L'API FIC espone due liste separate (cost_centers per documenti ricevuti, revenue_centers per documenti emessi); questo tool ne ritorna l'unione deduplicata e ordinata, coerente con la vista 'Analisi centri c/r' della UI FIC. Le validation interne dei tool di mutazione sono type-specific: create_invoice/credit_note/proforma/update_document/duplicate_invoice/convert_proforma_to_invoice validano `revenue_center` contro la sola lista revenue_centers; create_received_document valida `cost_center` contro la sola lista cost_centers. Read-only.",
            inputSchema={"type": "object", "properties": {}},
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="get_received_document",
            description="Dettaglio fattura passiva (ricevuta da fornitore) per ID. Read-only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID documento ricevuto"}
                },
                "required": ["document_id"]
            },
            annotations=_ann(read_only=True, idempotent=True),
        ),
        Tool(
            name="create_received_document",
            description="Crea documento passivo (fattura ricevuta o NDC ricevuta) registrando una spesa. IMPORTANTE: Chiedere conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "supplier_name": {"type": "string", "description": "Nome/Ragione sociale del fornitore"},
                    "supplier_vat_number": {"type": "string", "description": "Partita IVA del fornitore (opzionale)"},
                    "type": {"type": "string", "description": "Tipo: expense (default) o credit_note"},
                    "date": {"type": "string", "description": "Data documento YYYY-MM-DD (default: oggi)"},
                    "amount_net": {"type": "number", "description": "Importo netto"},
                    "amount_vat": {"type": "number", "description": "Importo IVA"},
                    "category": {"type": "string", "description": "Categoria spesa (opzionale)"},
                    "description": {"type": "string", "description": "Descrizione/oggetto (opzionale)"},
                    "cost_center": {"type": "string", "description": "Centro di costo (opzionale, deve esistere — vedi list_cost_centers)"}
                },
                "required": ["supplier_name", "amount_net"]
            },
            annotations=_ann(),
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "list_invoices":
            year = arguments.get("year", 2024)
            month = arguments.get("month")
            query = arguments.get("query")
            doc_type = arguments.get("type", "invoice")

            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            if month:
                last_day = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 29
                q = f"date >= '{year}-{month:02d}-01' and date <= '{year}-{month:02d}-{last_day}'"

            response = issued_api.list_issued_documents(
                company_id=COMPANY_ID, type=doc_type, q=q, per_page=100, fieldset="detailed"
            )
            invoices = []
            for doc in (response.data or []):
                d = doc.to_dict()
                inv = {
                    "id": d.get("id"),
                    "number": d.get("number"),
                    "date": str(d.get("date", "")),
                    "client": d.get("entity", {}).get("name") if d.get("entity") else None,
                    "total": get_total_from_doc(d),
                    "subject": d.get("subject"),
                    "description": d.get("visible_subject")
                }
                if d.get("rc_center"):
                    inv["revenue_center"] = d["rc_center"]
                if query:
                    search_text = f"{inv['client']} {inv['subject']} {inv['description']}".lower()
                    if query.lower() not in search_text:
                        continue
                invoices.append(inv)
            return [TextContent(type="text", text=json.dumps(invoices, indent=2, ensure_ascii=False))]

        elif name == "get_invoice":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            items = []
            for i in d.get("items_list", []):
                items.append({
                    "name": i.get("name"),
                    "description": i.get("description"),
                    "qty": i.get("qty"),
                    "net_price": i.get("net_price", 0),
                    "gross_price": i.get("gross_price", 0),
                    "vat": i.get("vat", {}).get("value") if i.get("vat") else None
                })
            payments = []
            for p in d.get("payments_list", []):
                pa = p.get("payment_account")
                if hasattr(pa, 'to_dict'):
                    pa = pa.to_dict()
                payments.append({
                    "amount": p.get("amount"),
                    "due_date": str(p.get("due_date", "")),
                    "status": str(p.get("status", "")).replace("IssuedDocumentStatus.", ""),
                    "paid_date": str(p.get("paid_date", "")) if p.get("paid_date") else None,
                    "payment_account_id": pa.get("id") if isinstance(pa, dict) else None
                })
            result = {
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "type": d.get("type"),
                "client_id": d.get("entity", {}).get("id") if d.get("entity") else None,
                "client": d.get("entity", {}).get("name") if d.get("entity") else None,
                "total": get_total_from_doc(d),
                "subject": d.get("subject"),
                "description": d.get("visible_subject"),
                "items": items,
                "payments": payments,
                "ei_status": d.get("ei_status"),
                "original_document": d.get("original_document")
            }
            if d.get("rc_center"):
                result["revenue_center"] = d["rc_center"]
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_pdf_url":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            attachment_url = d.get("attachment_url") or d.get("url") or ""
            web_url = f"https://secure.fattureincloud.it/issued-documents-view-{doc_id}"
            result = {
                "id": doc_id,
                "number": d.get("number"),
                "type": d.get("type", "invoice"),
                "client": d.get("entity", {}).get("name") if d.get("entity") else "",
                "attachment_url": attachment_url,
                "web_url": web_url,
                "note": "attachment_url è il PDF diretto (se disponibile). web_url apre il documento nel browser FIC."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_clients":
            query = arguments.get("query")
            response = clients_api.list_clients(company_id=COMPANY_ID, per_page=100)
            clients = []
            for c in (response.data or []):
                cd = c.to_dict()
                client = {"id": cd.get("id"), "name": cd.get("name"),
                          "vat": cd.get("vat_number"), "tax_code": cd.get("tax_code"),
                          "email": cd.get("email")}
                if query and query.lower() not in (client['name'] or '').lower():
                    continue
                clients.append(client)
            return [TextContent(type="text", text=json.dumps(clients, indent=2, ensure_ascii=False))]

        elif name == "get_client":
            cd = get_client_by_id(arguments["client_id"])
            if cd is None:
                return [TextContent(type="text", text=json.dumps({"error": "Cliente non trovato"}, ensure_ascii=False))]
            fields = ["id", "code", "name", "type", "first_name", "last_name",
                      "vat_number", "tax_code", "address_street", "address_postal_code",
                      "address_city", "address_province", "address_extra", "country",
                      "email", "certified_email", "phone", "ei_code"]
            client = {k: cd.get(k) for k in fields if cd.get(k) not in (None, "")}
            return [TextContent(type="text", text=json.dumps(client, indent=2, ensure_ascii=False, default=str))]

        elif name == "get_company_info":
            response = companies_api.get_company_info(company_id=COMPANY_ID)
            d = response.data.to_dict()
            info = d.get("info", d)
            result = {"name": info.get("name"), "vat": info.get("vat_number"),
                      "email": info.get("email"), "address": info.get("address_street"),
                      "city": info.get("address_city"), "province": info.get("address_province")}
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_client":
            client_data = {
                "name": arguments["name"],
                "vat_number": arguments.get("vat_number", ""),
                "tax_code": arguments.get("tax_code", ""),
                "ei_code": arguments.get("ei_code", ""),
                "certified_email": arguments.get("certified_email", ""),
                "email": arguments.get("email", ""),
                "address_street": arguments.get("address_street", ""),
                "address_city": arguments.get("address_city", ""),
                "address_postal_code": arguments.get("address_postal_code", ""),
                "address_province": arguments.get("address_province", ""),
                "country": arguments.get("country", "Italia"),
                "phone": arguments.get("phone", ""),
            }
            response = clients_api.create_client(
                company_id=COMPANY_ID,
                create_client_request={"data": client_data}
            )
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "name": d.get("name"),
                "vat_number": d.get("vat_number"),
                "message": f"Cliente '{d.get('name')}' creato con ID {d.get('id')}."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "update_client":
            client_id = arguments["client_id"]
            orig = get_client_by_id(client_id)
            if not orig:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": f"Cliente {client_id} non trovato"}, ensure_ascii=False))]
            fields = ["name", "vat_number", "tax_code", "ei_code", "certified_email",
                      "email", "address_street", "address_city", "address_postal_code",
                      "address_province", "phone"]
            client_data = {}
            for f in fields:
                if f in arguments:
                    client_data[f] = arguments[f]
                elif orig.get(f) is not None:
                    client_data[f] = orig[f]
            response = clients_api.modify_client(
                company_id=COMPANY_ID,
                client_id=client_id,
                modify_client_request={"data": client_data}
            )
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "name": d.get("name"),
                "message": f"Cliente '{d.get('name')}' aggiornato con successo."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_invoice":
            result, error = build_issued_document(
                doc_type="invoice",
                client_id=arguments["client_id"],
                items_data=arguments["items"],
                date_str=arguments.get("date", datetime.now().strftime("%Y-%m-%d")),
                payment_days=arguments.get("payment_days", 30),
                visible_subject=arguments.get("visible_subject", ""),
                revenue_center=arguments.get("revenue_center"),
            )
            if error:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": error}, ensure_ascii=False))]
            result["message"] = f"Fattura #{result['number']} creata come bozza. SDI: {result['ei_code']}. Usa send_to_sdi per inviarla."
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_credit_note":
            result, error = build_issued_document(
                doc_type="credit_note",
                client_id=arguments["client_id"],
                items_data=arguments["items"],
                date_str=arguments.get("date", datetime.now().strftime("%Y-%m-%d")),
                payment_days=arguments.get("payment_days", 30),
                visible_subject=arguments.get("visible_subject", ""),
                negate_prices=True,
                source_invoice_id=arguments.get("source_invoice_id"),
                revenue_center=arguments.get("revenue_center"),
            )
            if error:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": error}, ensure_ascii=False))]
            msg = f"NDC #{result['number']} creata come bozza. Totale: {result['total']}."
            if result.get("linked_to_invoice"):
                msg += f" Collegata a fattura ID {result['linked_to_invoice']}."
            msg += " Usa send_to_sdi per inviarla."
            result["message"] = msg
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_proforma":
            result, error = build_issued_document(
                doc_type="proforma",
                client_id=arguments["client_id"],
                items_data=arguments["items"],
                date_str=arguments.get("date", datetime.now().strftime("%Y-%m-%d")),
                payment_days=arguments.get("payment_days", 30),
                visible_subject=arguments.get("visible_subject", ""),
                revenue_center=arguments.get("revenue_center"),
            )
            if error:
                return [TextContent(type="text", text=json.dumps({"success": False, "error": error}, ensure_ascii=False))]
            result["message"] = f"Proforma #{result['number']} creata come bozza. Non inviabile allo SDI."
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "convert_proforma_to_invoice":
            doc_id = arguments["document_id"]
            keep_proforma = arguments.get("keep_proforma", False)

            orig_resp = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            orig = orig_resp.data.to_dict()

            if orig.get("type") != "proforma":
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Il documento {doc_id} non è una proforma (tipo: {orig.get('type')})"
                }, ensure_ascii=False))]

            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else None
            entity = build_entity_from_client(client_id, client_data) if (client_id and client_data) else orig.get("entity", {})

            date_str = arguments.get("date") or str(orig.get("date", datetime.now().strftime("%Y-%m-%d")))

            items_list = []
            for i in orig.get("items_list", []):
                items_list.append({
                    "name": i.get("name", ""),
                    "description": i.get("description", ""),
                    "qty": i.get("qty"),
                    "net_price": abs(i.get("net_price", 0)),
                    "vat": {"id": 0, "value": i.get("vat", {}).get("value", 22)}
                })

            orig_payments = orig.get("payments_list", [{}])
            payment_days = orig_payments[0].get("payment_terms", {}).get("days", 30) if orig_payments else 30

            invoice_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
            due_date = invoice_date + timedelta(days=payment_days)
            total_gross = sum(i["qty"] * i["net_price"] * (1 + i["vat"]["value"] / 100) for i in items_list)

            revenue_center = arguments.get("revenue_center") or orig.get("rc_center")
            if revenue_center:
                known = fetch_revenue_centers(company_id=COMPANY_ID)
                if revenue_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"revenue_center '{revenue_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            body_data = {
                "type": "invoice",
                "e_invoice": True,
                "ei_data": {"payment_method": "MP05"},
                "entity": entity,
                "date": date_str[:10],
                "visible_subject": orig.get("visible_subject", ""),
                "items_list": items_list,
                "payments_list": [{
                    "amount": round(total_gross, 2),
                    "due_date": due_date.strftime("%Y-%m-%d"),
                    "status": "not_paid",
                    "payment_terms": {"days": payment_days, "type": "standard"}
                }]
            }
            if revenue_center:
                body_data["rc_center"] = revenue_center
            body = {"data": body_data}

            response = issued_api.create_issued_document(
                company_id=COMPANY_ID, create_issued_document_request=body
            )
            d = response.data.to_dict()

            if not keep_proforma:
                issued_api.delete_issued_document(company_id=COMPANY_ID, document_id=doc_id)

            result = {
                "success": True,
                "invoice_id": d.get("id"),
                "invoice_number": d.get("number"),
                "date": date_str[:10],
                "due_date": due_date.strftime("%Y-%m-%d"),
                "client": (client_data or {}).get("name", entity.get("name", "")),
                "ei_code": entity.get("ei_code", "N/A"),
                "total": round(total_gross, 2),
                "proforma_deleted": not keep_proforma,
                "message": f"Fattura #{d.get('number')} creata da proforma #{orig.get('number')}. {'Proforma eliminata.' if not keep_proforma else 'Proforma mantenuta.'} Usa send_to_sdi per inviarla."
            }
            if revenue_center:
                result["revenue_center"] = revenue_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "update_document":
            doc_id = arguments["document_id"]

            orig_resp = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            orig = orig_resp.data.to_dict()

            current_status = orig.get("ei_status")
            if current_status and current_status not in [None, "not_sent"]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Impossibile modificare: documento già inviato allo SDI. Stato: {current_status}"
                }, ensure_ascii=False))]

            doc_type = orig.get("type", "invoice")
            is_credit_note = (doc_type == "credit_note")

            date_str = arguments.get("date") or str(orig.get("date", datetime.now().strftime("%Y-%m-%d")))
            visible_subject = arguments.get("visible_subject") if "visible_subject" in arguments else (orig.get("visible_subject") or "")

            if "items" in arguments:
                items_list = build_items_list(arguments["items"], negate=False)
            else:
                items_list = []
                for i in orig.get("items_list", []):
                    items_list.append({
                        "name": i.get("name", ""),
                        "description": i.get("description", ""),
                        "qty": i.get("qty"),
                        "net_price": abs(i.get("net_price", 0)),
                        "vat": {"id": 0, "value": i.get("vat", {}).get("value", 22)}
                    })

            if "payment_days" in arguments:
                payment_days = arguments["payment_days"]
            else:
                orig_payments = orig.get("payments_list", [{}])
                payment_days = orig_payments[0].get("payment_terms", {}).get("days", 30) if orig_payments else 30

            invoice_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
            due_date = invoice_date + timedelta(days=payment_days)
            total_abs = sum(
                abs(i["qty"] * i["net_price"]) * (1 + i["vat"]["value"] / 100)
                for i in items_list
            )
            result_total = -total_abs if is_credit_note else total_abs

            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else None
            entity = build_entity_from_client(client_id, client_data) if (client_id and client_data) else orig.get("entity", {})

            if "revenue_center" in arguments:
                revenue_center = arguments["revenue_center"]
            else:
                revenue_center = orig.get("rc_center")
            if revenue_center:
                known = fetch_revenue_centers(company_id=COMPANY_ID)
                if revenue_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"revenue_center '{revenue_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            body_data = {
                "type": doc_type,
                "entity": entity,
                "date": date_str[:10],
                "visible_subject": visible_subject,
                "items_list": items_list,
                "payments_list": [{
                    "amount": round(total_abs, 2),
                    "due_date": due_date.strftime("%Y-%m-%d"),
                    "status": "not_paid",
                    "payment_terms": {"days": payment_days, "type": "standard"}
                }]
            }
            if revenue_center:
                body_data["rc_center"] = revenue_center
            if doc_type in ("invoice", "credit_note"):
                body_data["e_invoice"] = True
                body_data["ei_data"] = {"payment_method": "MP05"}
            if orig.get("original_document"):
                body_data["original_document"] = orig["original_document"]

            response = issued_api.modify_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id,
                modify_issued_document_request={"data": body_data}
            )
            d = response.data.to_dict()

            result = {
                "success": True,
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "due_date": due_date.strftime("%Y-%m-%d"),
                "client": (client_data or {}).get("name", entity.get("name", "")),
                "total": round(result_total, 2),
                "type": doc_type,
                "status": "bozza",
                "message": f"Documento #{d.get('number')} aggiornato con successo."
            }
            if revenue_center:
                result["revenue_center"] = revenue_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "duplicate_invoice":
            source_id = arguments["source_document_id"]
            new_date_str = arguments.get("new_date", datetime.now().strftime("%Y-%m-%d"))
            desc_replace = arguments.get("description_replace", {})
            payment_days_override = arguments.get("payment_days")

            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=source_id, fieldset="detailed"
            )
            orig = response.data.to_dict()

            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else None
            entity = build_entity_from_client(client_id, client_data) if (client_id and client_data) else orig.get("entity", {})

            items_list = []
            for i in orig.get("items_list", []):
                iname = i.get("name", "")
                idesc = i.get("description", "")
                if desc_replace.get("old") and desc_replace.get("new"):
                    iname = iname.replace(desc_replace["old"], desc_replace["new"])
                    idesc = idesc.replace(desc_replace["old"], desc_replace["new"])
                items_list.append({
                    "name": iname, "description": idesc,
                    "qty": i.get("qty"), "net_price": i.get("net_price"),
                    "vat": {"id": 0, "value": i.get("vat", {}).get("value", 22)}
                })

            visible_subject = orig.get("visible_subject", "")
            if desc_replace.get("old") and desc_replace.get("new"):
                visible_subject = visible_subject.replace(desc_replace["old"], desc_replace["new"])

            invoice_date = datetime.strptime(new_date_str, "%Y-%m-%d")
            if payment_days_override is not None:
                payment_days = payment_days_override
            else:
                orig_payments = orig.get("payments_list", [{}])
                payment_days = orig_payments[0].get("payment_terms", {}).get("days", 30) if orig_payments else 30

            due_date = invoice_date + timedelta(days=payment_days)
            total_gross = sum(i["qty"] * i["net_price"] * (1 + i["vat"]["value"] / 100) for i in items_list)

            revenue_center = arguments.get("revenue_center") or orig.get("rc_center")
            if revenue_center:
                known = fetch_revenue_centers(company_id=COMPANY_ID)
                if revenue_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"revenue_center '{revenue_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            body_data = {
                "type": "invoice", "e_invoice": True, "ei_data": {"payment_method": "MP05"},
                "entity": entity, "date": new_date_str, "visible_subject": visible_subject,
                "items_list": items_list,
                "payments_list": [{"amount": round(total_gross, 2),
                                   "due_date": due_date.strftime("%Y-%m-%d"),
                                   "status": "not_paid",
                                   "payment_terms": {"days": payment_days, "type": "standard"}}]
            }
            if revenue_center:
                body_data["rc_center"] = revenue_center
            body = {"data": body_data}
            response = issued_api.create_issued_document(
                company_id=COMPANY_ID, create_issued_document_request=body
            )
            d = response.data.to_dict()
            result = {
                "success": True, "id": d.get("id"), "number": d.get("number"),
                "date": str(d.get("date", "")), "due_date": due_date.strftime("%Y-%m-%d"),
                "client": (client_data or {}).get("name", entity.get("name", "")),
                "ei_code": entity.get("ei_code", "N/A"), "total": round(total_gross, 2),
                "source_invoice": orig.get("number"), "status": "bozza",
                "message": f"Fattura #{d.get('number')} creata come bozza (duplicata da #{orig.get('number')}). Scadenza: {due_date.strftime('%d/%m/%Y')}."
            }
            if revenue_center:
                result["revenue_center"] = revenue_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "delete_invoice":
            doc_id = arguments["document_id"]
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            check_data = check.data.to_dict()
            current_status = check_data.get("ei_status")
            if current_status and current_status not in ["null", "not_sent", None]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Impossibile eliminare: documento già inviato allo SDI. Stato: {current_status}"
                }, ensure_ascii=False))]
            issued_api.delete_issued_document(company_id=COMPANY_ID, document_id=doc_id)
            result = {
                "success": True, "document_id": doc_id,
                "number": check_data.get("number"),
                "client": check_data.get("entity", {}).get("name"),
                "message": f"Documento #{check_data.get('number')} eliminato con successo."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "send_to_sdi":
            doc_id = arguments["document_id"]
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            check_data = check.data.to_dict()
            current_status = check_data.get("ei_status")
            if current_status and current_status not in ["null", "rejected", None, "not_sent"]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Documento già inviato o in elaborazione. Stato: {current_status}"
                }, ensure_ascii=False))]
            einvoice_api.send_e_invoice(
                company_id=COMPANY_ID, document_id=doc_id,
                send_e_invoice_request={"data": {"withholding_tax_causal": None}}
            )
            result = {
                "success": True, "document_id": doc_id,
                "number": check_data.get("number"),
                "client": check_data.get("entity", {}).get("name"),
                "message": f"Fattura #{check_data.get('number')} inviata allo SDI con successo!"
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_invoice_status":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            ei_status = d.get("ei_status")
            status_map = {
                None: "Bozza (non inviata)", "not_sent": "Bozza (non inviata)",
                "pending": "In attesa di invio", "sent": "Inviata, in attesa di risposta SDI",
                "delivered": "Consegnata al destinatario", "accepted": "Accettata",
                "rejected": "Rifiutata", "not_delivered": "Non consegnata (messa a disposizione)"
            }
            result = {
                "id": d.get("id"), "number": d.get("number"),
                "client": d.get("entity", {}).get("name"),
                "ei_status": ei_status,
                "ei_status_description": status_map.get(ei_status, ei_status),
                "date": str(d.get("date", ""))
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "send_email":
            if not SENDER_EMAIL:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": "❌ Sender email not configured. Open Claude Desktop → Extensions → FattureInCloud → Settings and set the 'Sender email' field, then retry the operation."
                }, ensure_ascii=False))]
            doc_id = arguments["document_id"]
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            check_data = check.data.to_dict()
            recipient_email = arguments.get("recipient_email") or check_data.get("entity", {}).get("email", "")
            if not recipient_email:
                return [TextContent(type="text", text=json.dumps({
                    "success": False, "error": "Nessuna email specificata e cliente senza email in anagrafica"
                }, ensure_ascii=False))]
            email_data = {"data": {
                "sender_email": SENDER_EMAIL, "recipient_email": recipient_email, "cc_email": "",
                "subject": arguments.get("subject") or f"Fattura n. {check_data.get('number')}",
                "body": arguments.get("body") or f"In allegato la fattura n. {check_data.get('number')}.\n\nCordiali saluti.",
                "include": {"document": True, "delivery_note": False, "attachment": False, "accompanying_invoice": False},
                "attach_pdf": True, "send_copy": False
            }}
            issued_api.schedule_email(
                company_id=COMPANY_ID, document_id=doc_id, schedule_email_request=email_data
            )
            result = {
                "success": True, "document_id": doc_id,
                "number": check_data.get("number"), "recipient": recipient_email,
                "message": f"Email con documento #{check_data.get('number')} inviata a {recipient_email}"
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_received_documents":
            year = arguments.get("year", datetime.now().year)
            month = arguments.get("month")
            doc_type = arguments.get("type", "expense")
            query = arguments.get("query")
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            if month:
                last_day = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 29
                q = f"date >= '{year}-{month:02d}-01' and date <= '{year}-{month:02d}-{last_day}'"
            response = received_api.list_received_documents(
                company_id=COMPANY_ID, type=doc_type, q=q, per_page=100, fieldset="detailed"
            )
            docs = []
            for doc in (response.data or []):
                d = doc.to_dict()
                supplier_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                desc = d.get('description', '') or ''
                if query and query.lower() not in f"{supplier_name} {desc}".lower():
                    continue
                entry = {
                    "id": d.get("id"), "number": d.get("number"),
                    "date": str(d.get("date", "")), "supplier": supplier_name,
                    "description": desc[:80], "total": d.get('amount_gross') or d.get('amount_net') or 0
                }
                if d.get("rc_center"):
                    entry["cost_center"] = d["rc_center"]
                docs.append(entry)
            return [TextContent(type="text", text=json.dumps(docs, indent=2, ensure_ascii=False))]

        elif name == "get_situation":
            year = arguments.get("year", datetime.now().year)
            client_filter = (arguments.get("client_name") or "").lower().strip()
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"

            emesse_resp = issued_api.list_issued_documents(
                company_id=COMPANY_ID, type="invoice", q=q, per_page=100, fieldset="detailed"
            )
            ndc_resp = issued_api.list_issued_documents(
                company_id=COMPANY_ID, type="credit_note", q=q, per_page=100, fieldset="detailed"
            )

            totale_fatturato = totale_incassato = totale_ndc = 0
            fatture_non_pagate = []

            for doc in (emesse_resp.data or []):
                d = doc.to_dict()
                client_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                if client_filter and client_filter not in client_name.lower():
                    continue
                totale_fatturato += get_total_from_doc(d)
                for p in d.get('payments_list', []):
                    status = str(p.get('status', '')).replace('IssuedDocumentStatus.', '')
                    if status == 'paid':
                        totale_incassato += p.get('amount', 0)
                    elif status == 'not_paid':
                        fatture_non_pagate.append({
                            "number": d.get("number"),
                            "client": client_name,
                            "amount": p.get('amount', 0),
                            "due_date": str(p.get('due_date', ''))
                        })

            for doc in (ndc_resp.data or []):
                d = doc.to_dict()
                client_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                if client_filter and client_filter not in client_name.lower():
                    continue
                totale_ndc += abs(get_total_from_doc(d))

            fatturato_netto = totale_fatturato - totale_ndc

            totale_costi = 0
            if not client_filter:
                ricevute_resp = received_api.list_received_documents(
                    company_id=COMPANY_ID, type="expense", q=q, per_page=100, fieldset="detailed"
                )
                totale_costi = sum(
                    d.to_dict().get('amount_gross') or d.to_dict().get('amount_net') or 0
                    for d in (ricevute_resp.data or [])
                )

            fatture_non_pagate.sort(key=lambda x: x.get('due_date', ''))
            result = {
                "anno": year,
                "filtro_cliente": client_filter or None,
                "fatturato_lordo": round(totale_fatturato, 2),
                "note_credito": round(totale_ndc, 2),
                "fatturato_netto": round(fatturato_netto, 2),
                "incassato": round(totale_incassato, 2),
                "da_incassare": round(fatturato_netto - totale_incassato, 2),
                "costi_totali": round(totale_costi, 2) if not client_filter else "N/A (filtro cliente attivo)",
                "margine_lordo": round(fatturato_netto - totale_costi, 2) if not client_filter else "N/A",
                "prossime_scadenze": fatture_non_pagate[:10]
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "check_numeration":
            year = arguments.get("year", datetime.now().year)
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            response = issued_api.list_issued_documents(
                company_id=COMPANY_ID, type="invoice", q=q, per_page=100
            )
            docs = [d.to_dict() for d in (response.data or [])]
            total_pages = getattr(response, 'last_page', 1) or 1
            if total_pages > 1:
                for page in range(2, total_pages + 1):
                    page_resp = issued_api.list_issued_documents(
                        company_id=COMPANY_ID, type="invoice", q=q, per_page=100, page=page
                    )
                    docs.extend([d.to_dict() for d in (page_resp.data or [])])
            if not docs:
                return [TextContent(type="text", text=json.dumps({
                    "year": year, "status": "Nessuna fattura trovata per questo anno"
                }, ensure_ascii=False))]
            numbers = sorted(set(
                d.get("number") for d in docs
                if d.get("number") is not None and d.get("number") > 0
            ))
            gaps = []
            if numbers:
                if numbers[0] != 1:
                    gaps.append({"type": "start", "expected": 1, "actual": numbers[0],
                                 "missing": list(range(1, numbers[0])),
                                 "note": f"La numerazione parte da {numbers[0]} invece che da 1"})
                for i in range(len(numbers) - 1):
                    if numbers[i + 1] - numbers[i] > 1:
                        missing = list(range(numbers[i] + 1, numbers[i + 1]))
                        gaps.append({"type": "gap", "after": numbers[i], "before": numbers[i + 1],
                                     "missing": missing,
                                     "note": f"Mancano i numeri {missing} tra fattura {numbers[i]} e {numbers[i+1]}"})
            result = {
                "year": year, "total_invoices": len(numbers),
                "first_number": numbers[0] if numbers else None,
                "last_number": numbers[-1] if numbers else None,
                "continuous": len(gaps) == 0,
                "status": "✓ Numerazione continua" if len(gaps) == 0 else f"⚠ Trovati {len(gaps)} problemi",
                "gaps": gaps
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_cost_centers":
            cost = fetch_cost_centers(company_id=COMPANY_ID)
            revenue = fetch_revenue_centers(company_id=COMPANY_ID)
            centers = sorted(set(cost) | set(revenue))
            return [TextContent(type="text", text=json.dumps(centers, indent=2, ensure_ascii=False))]

        elif name == "get_received_document":
            doc_id = arguments["document_id"]
            response = received_api.get_received_document(
                company_id=COMPANY_ID, document_id=doc_id, fieldset="detailed"
            )
            d = response.data.to_dict()
            items = []
            for i in d.get("items_list", []) or []:
                items.append({
                    "name": i.get("name"),
                    "description": i.get("description"),
                    "qty": i.get("qty"),
                    "net_price": i.get("net_price", 0),
                    "vat": i.get("vat", {}).get("value") if i.get("vat") else None
                })
            payments = []
            for p in d.get("payments_list", []) or []:
                payments.append({
                    "amount": p.get("amount"),
                    "due_date": str(p.get("due_date", "")),
                    "status": str(p.get("status", "")).replace("ReceivedDocumentStatus.", ""),
                    "paid_date": str(p.get("paid_date", "")) if p.get("paid_date") else None,
                })
            result = {
                "id": d.get("id"),
                "type": d.get("type"),
                "number": d.get("invoice_number"),
                "date": str(d.get("date", "")),
                "supplier": d.get("entity", {}).get("name") if d.get("entity") else None,
                "supplier_vat": d.get("entity", {}).get("vat_number") if d.get("entity") else None,
                "description": d.get("description"),
                "category": d.get("category"),
                "amount_net": d.get("amount_net"),
                "amount_vat": d.get("amount_vat"),
                "amount_gross": d.get("amount_gross"),
                "items": items,
                "payments": payments,
            }
            if d.get("rc_center"):
                result["cost_center"] = d["rc_center"]
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "create_received_document":
            cost_center = arguments.get("cost_center")
            if cost_center:
                known = fetch_cost_centers(company_id=COMPANY_ID)
                if cost_center not in known:
                    return [TextContent(type="text", text=json.dumps({
                        "success": False,
                        "error": f"cost_center '{cost_center}' non esiste. Disponibili: {known}."
                    }, ensure_ascii=False))]

            doc_type = arguments.get("type", "expense")
            date_str = arguments.get("date", datetime.now().strftime("%Y-%m-%d"))
            amount_net = arguments["amount_net"]
            amount_vat = arguments.get("amount_vat", 0)

            entity = {"name": arguments["supplier_name"]}
            if arguments.get("supplier_vat_number"):
                entity["vat_number"] = arguments["supplier_vat_number"]

            body_data = {
                "type": doc_type,
                "entity": entity,
                "date": date_str,
                "amount_net": amount_net,
                "amount_vat": amount_vat,
                "amount_gross": amount_net + amount_vat,
            }
            if arguments.get("category"):
                body_data["category"] = arguments["category"]
            if arguments.get("description"):
                body_data["description"] = arguments["description"]
            if cost_center:
                body_data["rc_center"] = cost_center

            response = received_api.create_received_document(
                company_id=COMPANY_ID,
                create_received_document_request={"data": body_data}
            )
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "type": d.get("type"),
                "supplier": d.get("entity", {}).get("name") if d.get("entity") else arguments["supplier_name"],
                "date": str(d.get("date", "")),
                "amount_gross": d.get("amount_gross") or (amount_net + amount_vat),
                "message": f"Documento ricevuto creato (ID {d.get('id')}).",
            }
            if cost_center:
                result["cost_center"] = cost_center
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        else:
            return [TextContent(type="text", text=f"Tool '{name}' non trovato")]

    except Exception as e:
        return [TextContent(type="text", text=f"Errore: {str(e)}\n{traceback.format_exc()}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
