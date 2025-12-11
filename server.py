#!/usr/bin/env python3
"""Fatture in Cloud MCP Server - v1.2

MCP Server per integrare Fatture in Cloud con Claude AI.
Permette di gestire fatture elettroniche italiane tramite conversazione.

Author: Mediaform s.c.r.l. (https://media-form.it)
License: MIT

"""

import json
import os
from datetime import datetime, timedelta

import fattureincloud_python_sdk as fic
from fattureincloud_python_sdk.api.issued_documents_api import IssuedDocumentsApi
from fattureincloud_python_sdk.api.issued_e_invoices_api import IssuedEInvoicesApi
from fattureincloud_python_sdk.api.received_documents_api import ReceivedDocumentsApi
from fattureincloud_python_sdk.api.clients_api import ClientsApi
from fattureincloud_python_sdk.api.companies_api import CompaniesApi

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Configurazione da variabili d'ambiente
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

app = Server("fattureincloud")


def get_total_from_doc(d):
    """Calcola totale documento da pagamenti o righe"""
    payments = d.get('payments_list', [])
    if payments:
        return sum(p.get('amount', 0) for p in payments)
    items = d.get('items_list', [])
    return sum((i.get('qty', 0) * i.get('gross_price', 0)) for i in items)


def get_client_by_id(client_id):
    """Recupera dati cliente per ID"""
    try:
        response = clients_api.get_client(company_id=COMPANY_ID, client_id=client_id)
        return response.data.to_dict()
    except:
        return None


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="list_invoices",
            description="Lista fatture emesse. Parametri: year (int), month (int opzionale), query (str opzionale)",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno (es. 2024)"},
                    "month": {"type": "integer", "description": "Mese 1-12 (opzionale)"},
                    "query": {"type": "string", "description": "Filtro testuale (opzionale)"}
                },
                "required": ["year"]
            }
        ),
        Tool(
            name="get_invoice",
            description="Dettaglio fattura per ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID fattura"}
                },
                "required": ["document_id"]
            }
        ),
        Tool(
            name="list_clients",
            description="Lista clienti",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filtro nome/ragione sociale (opzionale)"}
                }
            }
        ),
        Tool(
            name="get_company_info",
            description="Info azienda collegata",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="create_invoice",
            description="Crea nuova fattura (bozza). IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "client_id": {"type": "integer", "description": "ID cliente"},
                    "items": {
                        "type": "array",
                        "description": "Lista articoli",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Nome prodotto/servizio"},
                                "description": {"type": "string", "description": "Descrizione estesa"},
                                "qty": {"type": "number", "description": "Quantità"},
                                "net_price": {"type": "number", "description": "Prezzo netto unitario"},
                                "vat_rate": {"type": "number", "description": "Aliquota IVA (es. 22)"}
                            },
                            "required": ["name", "qty", "net_price"]
                        }
                    },
                    "date": {"type": "string", "description": "Data fattura YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "description": "Giorni pagamento (default: 30)"},
                    "visible_subject": {"type": "string", "description": "Oggetto visibile in fattura"}
                },
                "required": ["client_id", "items"]
            }
        ),
        Tool(
            name="duplicate_invoice",
            description="Duplica una fattura esistente con nuova data (crea bozza). IMPORTANTE: Chiedere sempre conferma all'utente prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_document_id": {"type": "integer", "description": "ID fattura da duplicare"},
                    "new_date": {"type": "string", "description": "Nuova data YYYY-MM-DD (default: oggi)"},
                    "payment_days": {"type": "integer", "description": "Giorni pagamento dalla data fattura (default: eredita da originale)"},
                    "description_replace": {
                        "type": "object",
                        "description": "Sostituzioni testo nella descrizione (es. 2025->2026)",
                        "properties": {
                            "old": {"type": "string"},
                            "new": {"type": "string"}
                        }
                    }
                },
                "required": ["source_document_id"]
            }
        ),
        Tool(
            name="delete_invoice",
            description="Elimina una fattura BOZZA (non inviata). ATTENZIONE: Azione irreversibile! Chiedere SEMPRE conferma esplicita all'utente. Funziona solo su fatture non ancora inviate allo SDI.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID fattura da eliminare"}
                },
                "required": ["document_id"]
            }
        ),
        Tool(
            name="send_to_sdi",
            description="Invia fattura allo SDI (Sistema di Interscambio). ATTENZIONE: Azione irreversibile! Chiedere SEMPRE conferma esplicita all'utente.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID fattura da inviare"}
                },
                "required": ["document_id"]
            }
        ),
        Tool(
            name="get_invoice_status",
            description="Controlla stato e-invoice/SDI di una fattura",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID fattura"}
                },
                "required": ["document_id"]
            }
        ),
        Tool(
            name="send_email",
            description="Invia copia cortesia fattura via email al cliente. IMPORTANTE: Chiedere conferma prima di eseguire.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "integer", "description": "ID fattura"},
                    "recipient_email": {"type": "string", "description": "Email destinatario (opzionale, usa email cliente se omesso)"},
                    "subject": {"type": "string", "description": "Oggetto email (opzionale)"},
                    "body": {"type": "string", "description": "Corpo email (opzionale)"}
                },
                "required": ["document_id"]
            }
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
            }
        ),
        Tool(
            name="get_situation",
            description="Dashboard anno: fatturato totale, incassato, da incassare, costi, margine",
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Anno (default: corrente)"}
                }
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "list_invoices":
            year = arguments.get("year", 2024)
            month = arguments.get("month")
            query = arguments.get("query")
            
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            if month:
                last_day = 31 if month in [1,3,5,7,8,10,12] else 30 if month in [4,6,9,11] else 29
                q = f"date >= '{year}-{month:02d}-01' and date <= '{year}-{month:02d}-{last_day}'"
            
            response = issued_api.list_issued_documents(
                company_id=COMPANY_ID,
                type="invoice",
                q=q,
                per_page=100,
                fieldset="detailed"
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
                if query:
                    search_text = f"{inv['client']} {inv['subject']} {inv['description']}".lower()
                    if query.lower() not in search_text:
                        continue
                invoices.append(inv)
            
            return [TextContent(type="text", text=json.dumps(invoices, indent=2, ensure_ascii=False))]
            
        elif name == "get_invoice":
            doc_id = arguments["document_id"]
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id,
                fieldset="detailed"
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
                payments.append({
                    "amount": p.get("amount"),
                    "due_date": str(p.get("due_date", "")),
                    "status": str(p.get("status", "")).replace("IssuedDocumentStatus.", ""),
                    "paid_date": str(p.get("paid_date", "")) if p.get("paid_date") else None
                })
            
            result = {
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "client_id": d.get("entity", {}).get("id") if d.get("entity") else None,
                "client": d.get("entity", {}).get("name") if d.get("entity") else None,
                "total": get_total_from_doc(d),
                "subject": d.get("subject"),
                "description": d.get("visible_subject"),
                "items": items,
                "payments": payments,
                "ei_status": d.get("ei_status")
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
            
        elif name == "list_clients":
            query = arguments.get("query")
            response = clients_api.list_clients(
                company_id=COMPANY_ID,
                per_page=100
            )
            clients = []
            for c in (response.data or []):
                cd = c.to_dict()
                client = {
                    "id": cd.get("id"), 
                    "name": cd.get("name"), 
                    "vat": cd.get("vat_number"), 
                    "tax_code": cd.get("tax_code"),
                    "email": cd.get("email")
                }
                if query:
                    search_text = f"{client['name']}".lower()
                    if query.lower() not in search_text:
                        continue
                clients.append(client)
            return [TextContent(type="text", text=json.dumps(clients, indent=2, ensure_ascii=False))]
            
        elif name == "get_company_info":
            response = companies_api.get_company_info(company_id=COMPANY_ID)
            d = response.data.to_dict()
            info = d.get("info", d)
            result = {
                "name": info.get("name"),
                "vat": info.get("vat_number"),
                "email": info.get("email"),
                "address": info.get("address_street"),
                "city": info.get("address_city"),
                "province": info.get("address_province")
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "create_invoice":
            client_id = arguments["client_id"]
            items_data = arguments["items"]
            date_str = arguments.get("date", datetime.now().strftime("%Y-%m-%d"))
            payment_days = arguments.get("payment_days", 30)
            visible_subject = arguments.get("visible_subject", "")
            
            client_data = get_client_by_id(client_id)
            if not client_data:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Cliente con ID {client_id} non trovato"
                }, indent=2, ensure_ascii=False))]
            
            items_list = []
            for item in items_data:
                vat_rate = item.get("vat_rate", 22)
                items_list.append({
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "qty": item["qty"],
                    "net_price": item["net_price"],
                    "vat": {"id": 0, "value": vat_rate}
                })
            
            invoice_date = datetime.strptime(date_str, "%Y-%m-%d")
            due_date = invoice_date + timedelta(days=payment_days)
            total_gross = sum(i["qty"] * i["net_price"] * (1 + i["vat"]["value"]/100) for i in items_list)
            
            body = {
                "data": {
                    "type": "invoice",
                    "e_invoice": True,
                    "ei_data": {"payment_method": "MP05"},
                    "entity": {
                        "id": client_id,
                        "name": client_data.get("name", ""),
                        "vat_number": client_data.get("vat_number", ""),
                        "tax_code": client_data.get("tax_code", ""),
                        "address_street": client_data.get("address_street", ""),
                        "address_city": client_data.get("address_city", ""),
                        "address_postal_code": client_data.get("address_postal_code", ""),
                        "address_province": client_data.get("address_province", ""),
                        "country": client_data.get("country", "Italia")
                    },
                    "date": date_str,
                    "visible_subject": visible_subject,
                    "items_list": items_list,
                    "payments_list": [{
                        "amount": round(total_gross, 2),
                        "due_date": due_date.strftime("%Y-%m-%d"),
                        "status": "not_paid",
                        "payment_terms": {"days": payment_days, "type": "standard"}
                    }]
                }
            }
            
            response = issued_api.create_issued_document(
                company_id=COMPANY_ID,
                create_issued_document_request=body
            )
            
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "client": client_data.get("name"),
                "total": round(total_gross, 2),
                "status": "bozza",
                "message": f"Fattura #{d.get('number')} creata come bozza. Usa send_to_sdi per inviarla."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "duplicate_invoice":
            source_id = arguments["source_document_id"]
            new_date_str = arguments.get("new_date", datetime.now().strftime("%Y-%m-%d"))
            desc_replace = arguments.get("description_replace", {})
            payment_days_override = arguments.get("payment_days")
            
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID,
                document_id=source_id,
                fieldset="detailed"
            )
            orig = response.data.to_dict()
            
            client_id = orig.get("entity", {}).get("id")
            client_data = get_client_by_id(client_id) if client_id else orig.get("entity", {})
            
            items_list = []
            for i in orig.get("items_list", []):
                name = i.get("name", "")
                desc = i.get("description", "")
                if desc_replace.get("old") and desc_replace.get("new"):
                    name = name.replace(desc_replace["old"], desc_replace["new"])
                    desc = desc.replace(desc_replace["old"], desc_replace["new"])
                items_list.append({
                    "name": name,
                    "description": desc,
                    "qty": i.get("qty"),
                    "net_price": i.get("net_price"),
                    "vat": {"id": 0, "value": i.get("vat", {}).get("value", 22)}
                })
            
            visible_subject = orig.get("visible_subject", "")
            if desc_replace.get("old") and desc_replace.get("new"):
                visible_subject = visible_subject.replace(desc_replace["old"], desc_replace["new"])
            
            invoice_date = datetime.strptime(new_date_str, "%Y-%m-%d")
            
            # Usa payment_days_override se fornito, altrimenti eredita dall'originale
            if payment_days_override is not None:
                payment_days = payment_days_override
            else:
                orig_payments = orig.get("payments_list", [{}])
                payment_days = orig_payments[0].get("payment_terms", {}).get("days", 30) if orig_payments else 30
            
            due_date = invoice_date + timedelta(days=payment_days)
            
            total_gross = sum(i["qty"] * i["net_price"] * (1 + i["vat"]["value"]/100) for i in items_list)
            
            body = {
                "data": {
                    "type": "invoice",
                    "e_invoice": True,
                    "ei_data": {"payment_method": "MP05"},
                    "entity": {
                        "id": client_id,
                        "name": client_data.get("name", ""),
                        "vat_number": client_data.get("vat_number", ""),
                        "tax_code": client_data.get("tax_code", ""),
                        "address_street": client_data.get("address_street", ""),
                        "address_city": client_data.get("address_city", ""),
                        "address_postal_code": client_data.get("address_postal_code", ""),
                        "address_province": client_data.get("address_province", ""),
                        "country": client_data.get("country", "Italia")
                    },
                    "date": new_date_str,
                    "visible_subject": visible_subject,
                    "items_list": items_list,
                    "payments_list": [{
                        "amount": round(total_gross, 2),
                        "due_date": due_date.strftime("%Y-%m-%d"),
                        "status": "not_paid",
                        "payment_terms": {"days": payment_days, "type": "standard"}
                    }]
                }
            }
            
            response = issued_api.create_issued_document(
                company_id=COMPANY_ID,
                create_issued_document_request=body
            )
            
            d = response.data.to_dict()
            result = {
                "success": True,
                "id": d.get("id"),
                "number": d.get("number"),
                "date": str(d.get("date", "")),
                "due_date": due_date.strftime("%Y-%m-%d"),
                "client": client_data.get("name"),
                "total": round(total_gross, 2),
                "source_invoice": orig.get("number"),
                "status": "bozza",
                "message": f"Fattura #{d.get('number')} creata come bozza (duplicata da #{orig.get('number')}). Scadenza: {due_date.strftime('%d/%m/%Y')}. Usa send_to_sdi per inviarla."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "delete_invoice":
            doc_id = arguments["document_id"]
            
            # Prima verifica che la fattura esista e sia una bozza
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id,
                fieldset="detailed"
            )
            check_data = check.data.to_dict()
            current_status = check_data.get("ei_status")
            
            # Permetti eliminazione solo se non inviata
            if current_status and current_status not in ["null", "not_sent", None]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Impossibile eliminare: fattura già inviata allo SDI. Stato attuale: {current_status}"
                }, indent=2, ensure_ascii=False))]
            
            # Elimina la fattura
            issued_api.delete_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id
            )
            
            result = {
                "success": True,
                "document_id": doc_id,
                "number": check_data.get("number"),
                "client": check_data.get("entity", {}).get("name"),
                "message": f"Fattura #{check_data.get('number')} eliminata con successo."
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "send_to_sdi":
            doc_id = arguments["document_id"]
            
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id,
                fieldset="detailed"
            )
            check_data = check.data.to_dict()
            current_status = check_data.get("ei_status")
            
            if current_status and current_status not in ["null", "rejected", None, "not_sent"]:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": f"Fattura già inviata o in elaborazione. Stato attuale: {current_status}"
                }, indent=2, ensure_ascii=False))]
            
            response = einvoice_api.send_e_invoice(
                company_id=COMPANY_ID,
                document_id=doc_id,
                send_e_invoice_request={"data": {"withholding_tax_causal": None}}
            )
            
            result = {
                "success": True,
                "document_id": doc_id,
                "number": check_data.get("number"),
                "client": check_data.get("entity", {}).get("name"),
                "message": f"Fattura #{check_data.get('number')} inviata allo SDI con successo!"
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "get_invoice_status":
            doc_id = arguments["document_id"]
            
            response = issued_api.get_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id,
                fieldset="detailed"
            )
            d = response.data.to_dict()
            
            ei_status = d.get("ei_status")
            status_map = {
                None: "Bozza (non inviata)",
                "not_sent": "Bozza (non inviata)",
                "pending": "In attesa di invio",
                "sent": "Inviata, in attesa di risposta SDI",
                "delivered": "Consegnata al destinatario",
                "accepted": "Accettata",
                "rejected": "Rifiutata",
                "not_delivered": "Non consegnata (messa a disposizione)"
            }
            
            result = {
                "id": d.get("id"),
                "number": d.get("number"),
                "client": d.get("entity", {}).get("name"),
                "ei_status": ei_status,
                "ei_status_description": status_map.get(ei_status, ei_status),
                "date": str(d.get("date", ""))
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        
        elif name == "send_email":
            doc_id = arguments["document_id"]
            recipient = arguments.get("recipient_email")
            subject = arguments.get("subject")
            body_text = arguments.get("body")
            
            check = issued_api.get_issued_document(
                company_id=COMPANY_ID,
                document_id=doc_id,
                fieldset="detailed"
            )
            check_data = check.data.to_dict()
            
            recipient_email = recipient or check_data.get("entity", {}).get("email", "")
            if not recipient_email:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": "Nessuna email specificata e cliente senza email in anagrafica"
                }, indent=2, ensure_ascii=False))]
            
            if not SENDER_EMAIL:
                return [TextContent(type="text", text=json.dumps({
                    "success": False,
                    "error": "FIC_SENDER_EMAIL non configurato. Imposta l'email mittente nel file .env"
                }, indent=2, ensure_ascii=False))]
            
            email_data = {
                "data": {
                    "sender_email": SENDER_EMAIL,
                    "recipient_email": recipient_email,
                    "cc_email": "",
                    "subject": subject or f"Fattura n. {check_data.get('number')}",
                    "body": body_text or f"In allegato la fattura n. {check_data.get('number')}.\n\nCordiali saluti.",
                    "include": {
                        "document": True,
                        "delivery_note": False,
                        "attachment": False,
                        "accompanying_invoice": False
                    },
                    "attach_pdf": True,
                    "send_copy": False
                }
            }
            
            response = issued_api.schedule_email(
                company_id=COMPANY_ID,
                document_id=doc_id,
                schedule_email_request=email_data
            )
            
            result = {
                "success": True,
                "document_id": doc_id,
                "number": check_data.get("number"),
                "recipient": recipient_email,
                "message": f"Email con fattura #{check_data.get('number')} inviata a {recipient_email}"
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
                company_id=COMPANY_ID,
                type=doc_type,
                q=q,
                per_page=100,
                fieldset="detailed"
            )
            
            docs = []
            for doc in (response.data or []):
                d = doc.to_dict()
                supplier_name = d.get('entity', {}).get('name', '') if d.get('entity') else ''
                desc = d.get('description', '') or ''
                
                if query:
                    search_text = f"{supplier_name} {desc}".lower()
                    if query.lower() not in search_text:
                        continue
                
                total = d.get('amount_gross') or d.get('amount_net') or 0
                
                docs.append({
                    "id": d.get("id"),
                    "number": d.get("number"),
                    "date": str(d.get("date", "")),
                    "supplier": supplier_name,
                    "description": desc[:80],
                    "total": total
                })
            
            return [TextContent(type="text", text=json.dumps(docs, indent=2, ensure_ascii=False))]
        
        elif name == "get_situation":
            year = arguments.get("year", datetime.now().year)
            
            q = f"date >= '{year}-01-01' and date <= '{year}-12-31'"
            
            # Fatture emesse
            emesse_resp = issued_api.list_issued_documents(
                company_id=COMPANY_ID,
                type="invoice",
                q=q,
                per_page=100,
                fieldset="detailed"
            )
            
            totale_fatturato = 0
            totale_incassato = 0
            fatture_non_pagate = []
            
            for doc in (emesse_resp.data or []):
                d = doc.to_dict()
                total = get_total_from_doc(d)
                totale_fatturato += total
                
                for p in d.get('payments_list', []):
                    status = str(p.get('status', '')).replace('IssuedDocumentStatus.', '')
                    if status == 'paid':
                        totale_incassato += p.get('amount', 0)
                    elif status == 'not_paid':
                        fatture_non_pagate.append({
                            "number": d.get("number"),
                            "client": d.get('entity', {}).get('name', '') if d.get('entity') else '',
                            "amount": p.get('amount', 0),
                            "due_date": str(p.get('due_date', ''))
                        })
            
            # Fatture ricevute (costi)
            ricevute_resp = received_api.list_received_documents(
                company_id=COMPANY_ID,
                type="expense",
                q=q,
                per_page=100,
                fieldset="detailed"
            )
            
            totale_costi = 0
            for doc in (ricevute_resp.data or []):
                d = doc.to_dict()
                totale_costi += d.get('amount_gross') or d.get('amount_net') or 0
            
            # Ordina scadenze per data
            fatture_non_pagate.sort(key=lambda x: x.get('due_date', ''))
            
            result = {
                "anno": year,
                "fatturato_totale": round(totale_fatturato, 2),
                "incassato": round(totale_incassato, 2),
                "da_incassare": round(totale_fatturato - totale_incassato, 2),
                "costi_totali": round(totale_costi, 2),
                "margine_lordo": round(totale_fatturato - totale_costi, 2),
                "prossime_scadenze": fatture_non_pagate[:10]
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
            
        else:
            return [TextContent(type="text", text=f"Tool {name} non trovato")]
            
    except Exception as e:
        import traceback
        return [TextContent(type="text", text=f"Errore: {str(e)}\n{traceback.format_exc()}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
