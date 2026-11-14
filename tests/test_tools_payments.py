"""Unit tests for the payment tools: list_payment_accounts, mark_payment_paid
and mark_payment_unpaid.

The interesting part of the feature is the body sent to FIC: the payment
status has to travel in the wire format the SDK accepts ('not_paid', not
'NOT_PAID'), and the cash account has to be resolved in a defined order
(explicit id, account already on the payment, first available).
"""

import asyncio
import importlib
import json
import sys

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def server_module(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FIC_CACHE_DISABLED", raising=False)
    monkeypatch.setenv("FIC_ACCESS_TOKEN", "test_token")
    monkeypatch.setenv("FIC_COMPANY_ID", "100")
    monkeypatch.setenv("FIC_SENDER_EMAIL", "test@example.invalid")

    for mod in ("server", "cache"):
        if mod in sys.modules:
            del sys.modules[mod]

    server = importlib.import_module("server")
    yield server


def _doc(payments, **extra):
    doc = {
        "id": 42,
        "number": 7,
        "date": "2026-03-26",
        "type": "invoice",
        "entity": {"id": 3, "name": "Client S.r.l."},
        "items_list": [{"name": "Item", "qty": 1, "net_price": 100, "vat": {"id": 0, "value": 22}}],
        "visible_subject": "Fattura",
        "payments_list": payments,
    }
    doc.update(extra)
    return doc


def _mock_doc(doc):
    response = MagicMock()
    response.data.to_dict.return_value = doc
    return response


def _mock_accounts(accounts):
    response = MagicMock()
    response.data = accounts
    return response


# --- fetch_payment_accounts ---------------------------------------------------

def test_fetch_payment_accounts_returns_accounts(server_module):
    server = server_module
    account = MagicMock()
    account.to_dict.return_value = {
        "id": 1252076, "name": "Contanti", "type": "PaymentAccountType.STANDARD"
    }
    response = _mock_accounts([account])

    with patch.object(server.info_api, "list_payment_accounts", return_value=response):
        result = server.fetch_payment_accounts(company_id=100)

    assert len(result) == 1
    assert result[0]["id"] == 1252076
    assert result[0]["name"] == "Contanti"
    assert result[0]["type"] == "STANDARD"


def test_fetch_payment_accounts_caches_repeated_calls(server_module):
    server = server_module
    response = _mock_accounts([])

    with patch.object(server.info_api, "list_payment_accounts", return_value=response) as m:
        for _ in range(5):
            server.fetch_payment_accounts(company_id=100)

    assert m.call_count == 1


def test_fetch_payment_accounts_handles_exception(server_module):
    server = server_module

    with patch.object(server.info_api, "list_payment_accounts", side_effect=Exception("boom")):
        assert server.fetch_payment_accounts(company_id=100) == []


# --- sanitize_payment --------------------------------------------------------

def test_sanitize_payment_lowercases_the_status(server_module):
    """The SDK only accepts the lowercase wire values, so 'NOT_PAID' would be
    rejected by the enum validation."""
    server = server_module

    cleaned = server.sanitize_payment({
        "id": 1,
        "amount": 100.0,
        "status": "IssuedDocumentStatus.NOT_PAID",
        "due_date": "2026-04-15",
    })

    assert cleaned["status"] == "not_paid"
    assert cleaned["amount"] == 100.0


def test_sanitize_payment_keeps_only_the_account_id(server_module):
    server = server_module

    cleaned = server.sanitize_payment({
        "status": "paid",
        "payment_account": {"id": 42, "name": "Contanti", "iban": "IT60"},
    })

    assert cleaned["payment_account"] == {"id": 42}


# --- payment_overview --------------------------------------------------------

def test_payment_overview_not_paid(server_module):
    server = server_module
    overview = server.payment_overview(_doc([
        {"id": 1, "amount": 100.0, "status": "not_paid", "due_date": "2026-04-15"}
    ]))

    assert overview["status"] == "not_paid"
    assert overview["paid"] is False
    assert overview["outstanding"] == 100.0
    assert overview["due_date"] == "2026-04-15"


def test_payment_overview_partially_paid(server_module):
    server = server_module
    overview = server.payment_overview(_doc([
        {"id": 1, "amount": 100.0, "status": "paid", "paid_date": "2026-04-01"},
        {"id": 2, "amount": 50.0, "status": "not_paid", "due_date": "2026-05-01"},
    ]))

    assert overview["status"] == "partially_paid"
    assert overview["outstanding"] == 50.0


# --- mark_payment_paid -------------------------------------------------------

def test_mark_payment_paid_sets_status_date_and_account(server_module):
    server = server_module
    doc = _doc([{"id": 1, "amount": 100.0, "status": "not_paid", "due_date": "2026-04-15"}])

    with patch.object(server.issued_api, "get_issued_document", return_value=_mock_doc(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = asyncio.run(server.call_tool("mark_payment_paid", {
            "document_id": 42, "paid_date": "2026-04-10", "payment_account_id": 777
        }))

    payload = json.loads(result[0].text)
    assert payload["success"] is True
    assert payload["status"] == "paid"
    assert payload["paid_date"] == "2026-04-10"
    assert payload["payment_account_id"] == 777

    body = modify.call_args.kwargs["modify_issued_document_request"]["data"]
    payment = body["payments_list"][0]
    assert payment["status"] == "paid"
    assert payment["paid_date"] == "2026-04-10"
    assert payment["payment_account"] == {"id": 777}
    # the rest of the document is preserved
    assert body["entity"] == doc["entity"]
    assert body["items_list"] == doc["items_list"]
    assert body["type"] == "invoice"
    assert body["e_invoice"] is True


def test_mark_payment_paid_reuses_the_account_already_on_the_payment(server_module):
    server = server_module
    doc = _doc([{
        "id": 1, "amount": 100.0, "status": "not_paid",
        "payment_account": {"id": 555, "name": "Tot"},
    }])

    with patch.object(server.issued_api, "get_issued_document", return_value=_mock_doc(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = asyncio.run(server.call_tool("mark_payment_paid", {"document_id": 42}))

    payload = json.loads(result[0].text)
    assert payload["payment_account_id"] == 555
    body = modify.call_args.kwargs["modify_issued_document_request"]["data"]
    assert body["payments_list"][0]["payment_account"] == {"id": 555}


def test_mark_payment_paid_falls_back_to_the_first_available_account(server_module):
    server = server_module
    doc = _doc([{"id": 1, "amount": 100.0, "status": "not_paid"}])
    account = MagicMock()
    account.to_dict.return_value = {"id": 111, "name": "Contanti"}

    with patch.object(server.issued_api, "get_issued_document", return_value=_mock_doc(doc)), \
         patch.object(server.info_api, "list_payment_accounts", return_value=_mock_accounts([account])), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = asyncio.run(server.call_tool("mark_payment_paid", {"document_id": 42}))

    payload = json.loads(result[0].text)
    assert payload["payment_account_id"] == 111
    body = modify.call_args.kwargs["modify_issued_document_request"]["data"]
    assert body["payments_list"][0]["payment_account"] == {"id": 111}


def test_mark_payment_paid_without_any_account_fails_cleanly(server_module):
    server = server_module
    doc = _doc([{"id": 1, "amount": 100.0, "status": "not_paid"}])

    with patch.object(server.issued_api, "get_issued_document", return_value=_mock_doc(doc)), \
         patch.object(server.info_api, "list_payment_accounts", return_value=_mock_accounts([])), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = asyncio.run(server.call_tool("mark_payment_paid", {"document_id": 42}))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "Nessun conto di pagamento" in payload["error"]
    modify.assert_not_called()


def test_mark_payment_paid_on_unknown_payment_index_fails_cleanly(server_module):
    server = server_module
    doc = _doc([{"id": 1, "amount": 100.0, "status": "not_paid"}])

    with patch.object(server.issued_api, "get_issued_document", return_value=_mock_doc(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = asyncio.run(server.call_tool("mark_payment_paid", {
            "document_id": 42, "payment_index": 3
        }))

    payload = json.loads(result[0].text)
    assert payload["success"] is False
    assert "non trovata" in payload["error"]
    modify.assert_not_called()


# --- mark_payment_unpaid -----------------------------------------------------

def test_mark_payment_unpaid_clears_date_and_account(server_module):
    server = server_module
    doc = _doc([{
        "id": 1, "amount": 100.0, "status": "paid", "paid_date": "2026-04-10",
        "payment_account": {"id": 777},
    }])

    with patch.object(server.issued_api, "get_issued_document", return_value=_mock_doc(doc)), \
         patch.object(server.issued_api, "modify_issued_document") as modify:
        result = asyncio.run(server.call_tool("mark_payment_unpaid", {"document_id": 42}))

    payload = json.loads(result[0].text)
    assert payload["success"] is True
    assert payload["status"] == "not_paid"

    body = modify.call_args.kwargs["modify_issued_document_request"]["data"]
    payment = body["payments_list"][0]
    assert payment["status"] == "not_paid"
    assert "paid_date" not in payment
    assert "payment_account" not in payment


# --- list_invoices exposes the payment state ---------------------------------

def test_list_invoices_exposes_the_payment_state(server_module):
    server = server_module
    doc = _doc([{"id": 1, "amount": 100.0, "status": "not_paid", "due_date": "2026-04-15"}])
    listed = MagicMock()
    listed.data = [MagicMock()]
    listed.data[0].to_dict.return_value = doc

    with patch.object(server.issued_api, "list_issued_documents", return_value=listed):
        result = asyncio.run(server.call_tool("list_invoices", {"year": 2026}))

    payload = json.loads(result[0].text)
    assert payload[0]["status"] == "not_paid"
    assert payload[0]["paid"] is False
    assert payload[0]["due_date"] == "2026-04-15"
    assert payload[0]["outstanding"] == 100.0


# --- list_payment_accounts tool ----------------------------------------------

def test_list_payment_accounts_tool(server_module):
    server = server_module
    account = MagicMock()
    account.to_dict.return_value = {"id": 111, "name": "Contanti", "type": "PaymentAccountType.STANDARD"}

    with patch.object(server.info_api, "list_payment_accounts", return_value=_mock_accounts([account])):
        result = asyncio.run(server.call_tool("list_payment_accounts", {}))

    payload = json.loads(result[0].text)
    assert payload["count"] == 1
    assert payload["payment_accounts"][0]["name"] == "Contanti"
