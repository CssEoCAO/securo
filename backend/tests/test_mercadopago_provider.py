"""Mercado Pago provider: paste parsing, row mapping and balance roll-forward."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.providers.base import ProviderUserActionRequired
from app.providers.mercadopago import (
    MercadoPagoProvider,
    parse_paste,
    row_description,
    row_id,
)

BRT = timezone(timedelta(hours=-3))


def _row(ts: datetime, amount: str, kind="SETTLEMENT", tags="", method="", source="1"):
    return {
        "TRANSACTION_DATE": ts.isoformat(),
        "SOURCE_ID": source,
        "TRANSACTION_TYPE": kind,
        "TRANSACTION_AMOUNT": amount,
        "SETTLEMENT_NET_AMOUNT": amount,
        "TRANSACTION_CURRENCY": "BRL",
        "PAYMENT_METHOD": method,
        "OPERATION_TAGS": tags,
        "DESCRIPTION": "",
    }


@pytest.mark.parametrize(
    "text,token,balance",
    [
        ("APP_USR-abc-123 1234,56", "APP_USR-abc-123", Decimal("1234.56")),
        ("APP_USR-abc-123\n1.234,56", "APP_USR-abc-123", Decimal("1234.56")),
        ("APP_USR-abc-123 saldo: R$ 10.50", "APP_USR-abc-123", Decimal("10.50")),
        ("APP_USR-abc-123", "APP_USR-abc-123", None),
        ("qualquer coisa", "", None),
    ],
)
def test_parse_paste(text, token, balance):
    assert parse_paste(text) == (token, balance)


def test_descriptions():
    now = datetime.now(BRT)
    assert row_description(_row(now, "50", tags='[{"TRACK":"PIX"}]')) == "Pix recebido"
    assert row_description(_row(now, "-50", tags='[{"TRACK":"PIX"}]')) == "Pix enviado"
    assert row_description(_row(now, "-50", kind="PAYOUTS")) == "Saque / transferência"
    assert row_description(_row(now, "3", kind="CASHBACK")) == "Cashback"
    assert row_description(_row(now, "-20", method="account_money")) == "Pagamento (saldo Mercado Pago)"
    assert row_description(_row(now, "-20", tags='[{"Utility_ID":"1"}]')) == "Pagamento de conta"


def test_row_id_is_stable_and_distinct():
    now = datetime.now(BRT)
    a = _row(now, "10", source="9")
    assert row_id(a) == row_id(dict(a))
    assert row_id(a) != row_id(_row(now, "10", kind="REFUND", source="9"))


@pytest.mark.asyncio
async def test_connect_requires_balance():
    with pytest.raises(ProviderUserActionRequired) as exc:
        await MercadoPagoProvider().handle_oauth_callback("APP_USR-abc")
    assert exc.value.code == "balance_missing"


@pytest.mark.asyncio
async def test_balance_rolls_forward_once_per_movement(monkeypatch):
    connected = datetime.now(timezone.utc) - timedelta(days=2)
    before = _row((connected - timedelta(hours=1)).astimezone(BRT), "999", source="old")
    after1 = _row((connected + timedelta(hours=1)).astimezone(BRT), "100", source="a")
    after2 = _row((connected + timedelta(hours=2)).astimezone(BRT), "-30", source="b")
    reports = [[before, after1], [before, after1, after2]]

    p = MercadoPagoProvider()
    monkeypatch.setattr(MercadoPagoProvider, "_token", staticmethod(lambda c: "APP_USR-x"))

    async def fake_rows(self, token, begin, end):
        return reports.pop(0)

    monkeypatch.setattr(MercadoPagoProvider, "_report_rows", fake_rows)
    creds = {"connected_at": connected.isoformat(), "balance": "500", "counted": {}, "user_id": "1", "nickname": "x"}

    creds = await p.refresh_credentials(creds)
    assert Decimal(creds["balance"]) == Decimal("600")       # only the row after connect
    creds = await p.refresh_credentials(creds)                 # a late row shows up, the old one repeats
    assert Decimal(creds["balance"]) == Decimal("570")
    accounts = await p.get_accounts(creds)
    assert accounts[0].balance == Decimal("570") and accounts[0].currency == "BRL"


@pytest.mark.asyncio
async def test_transactions_mapping(monkeypatch):
    now = datetime.now(BRT)
    rows = [
        _row(now - timedelta(days=1), "-42.5", tags='[{"TRACK":"PIX"}]', source="p"),
        _row(now - timedelta(days=1), "0", source="z"),
        _row(now - timedelta(days=2), "10", kind="CASHBACK", source="c"),
    ]
    monkeypatch.setattr(MercadoPagoProvider, "_token", staticmethod(lambda c: "APP_USR-x"))

    async def fake_rows(self, token, begin, end):
        return rows

    async def no_lookup(self, token, source_id):
        return None

    monkeypatch.setattr(MercadoPagoProvider, "_report_rows", fake_rows)
    monkeypatch.setattr(MercadoPagoProvider, "_payment_description", no_lookup)
    txs = await MercadoPagoProvider().get_transactions({}, "mp-1", None)
    assert [(t.type, t.amount, t.description) for t in txs] == [
        ("debit", Decimal("42.5"), "Pix enviado"),
        ("credit", Decimal("10"), "Cashback"),
    ]
    assert all(t.external_id.startswith("mp:") for t in txs)
