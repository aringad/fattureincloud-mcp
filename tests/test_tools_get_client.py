"""Unit tests for the get_client tool."""

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


def _client_response(data):
    response = MagicMock()
    response.data.to_dict.return_value = data
    return response


def _call(server, arguments):
    result = asyncio.run(server.call_tool("get_client", arguments))
    return json.loads(result[0].text)


def test_get_client_returns_registry_fields(server_module):
    server = server_module
    data = {
        "id": 42, "name": "ACME S.R.L.", "type": "company",
        "vat_number": "01234567890", "tax_code": "01234567890",
        "address_street": "Via Roma 1", "address_postal_code": "25015",
        "address_city": "Desenzano del Garda", "address_province": "BS",
        "certified_email": "acme@pec.it", "ei_code": "ABC1234",
        "notes": "internal", "default_payment_method": None,
    }

    with patch.object(server.clients_api, "get_client", return_value=_client_response(data)):
        client = _call(server, {"client_id": 42})

    assert client["address_city"] == "Desenzano del Garda"
    assert client["certified_email"] == "acme@pec.it"
    assert client["ei_code"] == "ABC1234"
    assert "notes" not in client
    assert "default_payment_method" not in client


def test_get_client_not_found(server_module):
    server = server_module

    with patch.object(server.clients_api, "get_client", side_effect=Exception("404")):
        result = _call(server, {"client_id": 999})

    assert result == {"error": "Cliente non trovato"}
