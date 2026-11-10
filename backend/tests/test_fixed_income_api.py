"""Fixed income (renda fixa) through the assets API."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.app_clock import app_today


async def _manual_account(client, headers, name="Corretora"):
    r = await client.post("/api/accounts", headers=headers, json={"name": name, "type": "checking", "currency": "BRL", "balance": 0})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _txs(client, headers, account_id):
    r = await client.get("/api/transactions", headers=headers, params={"account_id": account_id, "limit": 200})
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    return [t for t in items if t.get("source") == "fixed_income" or (t.get("description") or "").startswith(("Rendimento", "Resgate"))]


@pytest.mark.asyncio
async def test_monthly_coupons_post_once(client: AsyncClient, auth_headers: dict):
    acc = await _manual_account(client, auth_headers)
    start = app_today() - timedelta(days=95)
    r = await client.post("/api/assets", headers=auth_headers, json={
        "name": "allu", "type": "investment", "currency": "BRL",
        "valuation_method": "fixed_income", "purchase_price": 10000, "purchase_date": start.isoformat(),
        "maturity_date": (start + timedelta(days=730)).isoformat(),
        "fi_index": "prefixado", "fi_rate": 25, "fi_payment": "monthly", "fi_tax": "regressiva",
        "fi_payout_account_id": acc,
    })
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["valuation_method"] == "fixed_income" and a["fi_rate"] == 25
    fi = a["fixed_income"]
    assert fi["payments"] == 3 and not fi["matured"]
    assert Decimal(fi["net_value"]) < Decimal(fi["gross_value"])
    assert a["current_value"] == pytest.approx(float(fi["net_value"]))
    txs = await _txs(client, auth_headers, acc)
    assert len(txs) == 3 and all(t["type"] == "credit" for t in txs)

    # editing (any field) resyncs without duplicating payouts
    r = await client.patch(f"/api/assets/{a['id']}", headers=auth_headers, json={"name": "allu 25%"})
    assert r.status_code == 200, r.text
    assert len(await _txs(client, auth_headers, acc)) == 3


@pytest.mark.asyncio
async def test_rejects_synced_payout_account(client: AsyncClient, auth_headers: dict, test_account):
    r = await client.post("/api/assets", headers=auth_headers, json={
        "name": "CDB", "type": "investment", "currency": "BRL", "valuation_method": "fixed_income",
        "purchase_price": 1000, "purchase_date": (app_today() - timedelta(days=10)).isoformat(),
        "fi_index": "prefixado", "fi_rate": 12, "fi_payment": "maturity",
        "fi_payout_account_id": str(test_account.id),
    })
    assert r.status_code in (404, 422), r.text


@pytest.mark.asyncio
async def test_maturity_closes_asset_and_pays_net(client: AsyncClient, auth_headers: dict):
    acc = await _manual_account(client, auth_headers, "Banco")
    start = app_today() - timedelta(days=400)
    r = await client.post("/api/assets", headers=auth_headers, json={
        "name": "LCI", "type": "investment", "currency": "BRL", "valuation_method": "fixed_income",
        "purchase_price": 1000, "purchase_date": start.isoformat(),
        "maturity_date": (start + timedelta(days=365)).isoformat(),
        "fi_index": "prefixado", "fi_rate": 10, "fi_payment": "maturity", "fi_tax": "isento",
        "fi_payout_account_id": acc,
    })
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["fixed_income"]["matured"] and a["sell_date"] == (start + timedelta(days=365)).isoformat()
    assert a["sell_price"] == pytest.approx(1100.0, abs=0.01)
    txs = await _txs(client, auth_headers, acc)
    assert len(txs) == 1 and float(txs[0]["amount"]) == pytest.approx(1100.0, abs=0.01)


@pytest.mark.asyncio
async def test_invalid_params_are_422(client: AsyncClient, auth_headers: dict):
    r = await client.post("/api/assets", headers=auth_headers, json={
        "name": "x", "type": "investment", "currency": "BRL", "valuation_method": "fixed_income",
        "purchase_price": 1000, "purchase_date": app_today().isoformat(),
        "fi_index": "prefixado", "fi_rate": 10, "fi_payment": "monthly",  # periodic needs maturity
    })
    assert r.status_code == 422
    assert "vencimento" in r.text


@pytest.mark.asyncio
async def test_cdi_uses_bcb_series(client: AsyncClient, auth_headers: dict, monkeypatch):
    from app.services import fixed_income_service

    calls = []

    async def fake_fetch(series, a, b):
        calls.append((series, a, b))
        out, d = [], a
        while d <= b:
            if d.weekday() < 5:
                out.append((d, Decimal("0.05")))
            d += timedelta(days=1)
        return out

    monkeypatch.setattr(fixed_income_service, "_fetch_sgs", fake_fetch)
    start = app_today() - timedelta(days=30)
    r = await client.post("/api/assets", headers=auth_headers, json={
        "name": "Cofrinho", "type": "investment", "currency": "BRL", "valuation_method": "fixed_income",
        "purchase_price": 1000, "purchase_date": start.isoformat(),
        "fi_index": "cdi", "fi_rate": 100, "fi_payment": "maturity", "fi_tax": "regressiva",
    })
    assert r.status_code == 201, r.text
    assert calls and calls[0][0] == "cdi"
    gross = Decimal(r.json()["fixed_income"]["gross_value"])
    business_days = sum(1 for i in range(1, 31) if (start + timedelta(days=i)).weekday() < 5)
    expected = (Decimal(1000) * (1 + Decimal("0.0005")) ** business_days).quantize(Decimal("0.01"))
    assert gross == expected
