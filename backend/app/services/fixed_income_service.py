"""Fixed income (renda fixa) on top of assets: indexes, values and payouts.

`sync_fixed_income_asset` is idempotent and runs on create/update and from the
daily task. It recomputes the position from the engine
(`app/services/fixed_income.py`), rewrites the asset's value history (net of
the IR due if redeemed that day), posts each coupon / redemption as a credit
in the payout account (once, keyed by asset and date) and closes the asset at
maturity. CDI and IPCA come from the Banco Central SGS API and are cached in
`index_rates`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.index_rate import IndexRate
from app.models.transaction import Transaction
from app.services import fixed_income as fi

logger = logging.getLogger(__name__)

SGS = {"cdi": 12, "ipca": 433}
SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
MAX_SPAN_DAYS = 3650  # SGS caps daily series at 10 years per request


class FixedIncomeError(ValueError):
    pass


def params_of(asset: Asset) -> fi.Params:
    if asset.purchase_price is None or asset.purchase_date is None:
        raise FixedIncomeError("Renda fixa precisa do valor aplicado e da data da aplicação.")
    p = fi.Params(
        principal=Decimal(str(asset.purchase_price)),
        start=asset.purchase_date,
        index=asset.fi_index or "prefixado",
        rate=Decimal(str(asset.fi_rate or 0)),
        payment=asset.fi_payment or "maturity",
        maturity=asset.maturity_date,
        tax=asset.fi_tax or "regressiva",
        tax_rate=Decimal(str(asset.fi_tax_rate)) if asset.fi_tax_rate is not None else None,
    )
    try:
        p.validate()
    except ValueError as exc:
        raise FixedIncomeError(str(exc)) from exc
    return p


async def _fetch_sgs(series: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    out: list[tuple[date, Decimal]] = []
    cursor = start
    async with httpx.AsyncClient(timeout=30.0) as client:
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=MAX_SPAN_DAYS))
            resp = await client.get(
                SGS_URL.format(code=SGS[series]),
                params={"formato": "json", "dataInicial": cursor.strftime("%d/%m/%Y"),
                        "dataFinal": chunk_end.strftime("%d/%m/%Y")},
            )
            if resp.status_code == 404:  # no data in range
                cursor = chunk_end + timedelta(days=1)
                continue
            resp.raise_for_status()
            for row in resp.json() or []:
                d = datetime.strptime(row["data"], "%d/%m/%Y").date()
                out.append((d, Decimal(str(row["valor"]))))
            cursor = chunk_end + timedelta(days=1)
    return out


async def ensure_indexes(session: AsyncSession, series: str, since: date) -> None:
    """Fetch what is missing of `series` from `since` to today (best effort)."""
    today = app_today()
    first, last = (
        await session.execute(
            select(func.min(IndexRate.date), func.max(IndexRate.date)).where(IndexRate.series == series)
        )
    ).one()
    ranges = []
    if first is None:
        ranges.append((since, today))
    else:
        if since < first:
            ranges.append((since, first - timedelta(days=1)))
        if last < today - timedelta(days=1):
            ranges.append((last + timedelta(days=1), today))
    for a, b in ranges:
        try:
            rows = await _fetch_sgs(series, a, b)
        except Exception:
            logger.warning("BCB %s fetch %s..%s failed", series, a, b, exc_info=True)
            continue
        for d, v in rows:
            await session.merge(IndexRate(series=series, date=d, value=v))
    await session.flush()


async def load_indexes(session: AsyncSession, p: fi.Params) -> fi.Indexes:
    cdi: dict[date, Decimal] = {}
    ipca: dict[tuple[int, int], Decimal] = {}
    for series in fi.needed_series(p):
        await ensure_indexes(session, series, p.start - timedelta(days=40))
        rows = (
            await session.execute(
                select(IndexRate.date, IndexRate.value).where(
                    IndexRate.series == series, IndexRate.date >= p.start - timedelta(days=40)
                )
            )
        ).all()
        for d, v in rows:
            if series == "cdi":
                cdi[d] = Decimal(str(v))
            else:
                ipca[(d.year, d.month)] = Decimal(str(v))
    return fi.Indexes(cdi=cdi, ipca=ipca)


async def validate_payout_account(
    session: AsyncSession, workspace_id: uuid.UUID, account_id: Optional[uuid.UUID]
) -> None:
    if account_id is None:
        return
    acc = await session.get(Account, account_id)
    if acc is None or acc.workspace_id != workspace_id:
        raise FixedIncomeError("Conta de destino não encontrada.")
    if acc.connection_id is not None:
        raise FixedIncomeError(
            "A conta de destino precisa ser manual: numa conta sincronizada com o banco "
            "o pagamento já chega pela sincronização e ficaria duplicado."
        )


def _payment_text(asset: Asset, pay: fi.Payment) -> tuple[str, str]:
    what = "Resgate" if pay.kind == "vencimento" else "Rendimento"
    note = (
        f"Bruto {pay.gross_income} · IR {pay.ir} ({pay.ir_rate * 100:.1f}%)"
        + (f" · principal {pay.principal}" if pay.principal else "")
    )
    return f"{what} {asset.name}"[:500], note


async def sync_fixed_income_asset(session: AsyncSession, asset: Asset, today: Optional[date] = None) -> fi.Position:
    today = today or app_today()
    p = params_of(asset)
    ix = await load_indexes(session, p)
    pos = fi.position(p, ix, today)

    # value history: rewrite (it is fully derived)
    await session.execute(delete(AssetValue).where(AssetValue.asset_id == asset.id))
    for d, v in fi.value_points(p, ix, today):
        session.add(AssetValue(asset_id=asset.id, amount=v, date=d, source="rule"))

    # payouts into the manual account, once each
    if asset.fi_payout_account_id:
        acc = await session.get(Account, asset.fi_payout_account_id)
        if acc is not None and acc.connection_id is None:
            for pay in pos.payments:
                ext = f"fi:{asset.id}:{pay.date.isoformat()}"
                exists = (
                    await session.execute(
                        select(Transaction.id).where(
                            Transaction.account_id == acc.id, Transaction.external_id == ext
                        )
                    )
                ).first()
                if exists or pay.net <= 0:
                    continue
                desc, note = _payment_text(asset, pay)
                session.add(Transaction(
                    user_id=asset.user_id,
                    workspace_id=asset.workspace_id,
                    account_id=acc.id,
                    external_id=ext,
                    description=desc,
                    original_description=desc,
                    amount=pay.net,
                    currency=acc.currency,
                    date=pay.date,
                    effective_date=pay.date,
                    type="credit",
                    source="fixed_income",
                    status="posted",
                    notes=note,
                    raw_data={"asset_id": str(asset.id), "kind": pay.kind,
                              "gross_income": str(pay.gross_income), "ir": str(pay.ir),
                              "principal": str(pay.principal)},
                ))

    if pos.matured:
        last = pos.payments[-1]
        final = last.net if p.payment == "maturity" else p.principal
        if asset.sell_date is None:
            asset.sell_date = last.date
            asset.sell_price = final
        # the final value point closes the chart at the redemption amount
        session.add(AssetValue(asset_id=asset.id, amount=final, date=last.date, source="rule"))

    meta = dict(asset.external_metadata or {})
    meta["fixed_income"] = {
        "as_of": pos.as_of.isoformat(),
        "gross_value": str(pos.gross_value),
        "ir": str(pos.ir),
        "ir_rate": str(pos.ir_rate),
        "net_value": str(pos.net_value),
        "paid_net": str(pos.paid_net),
        "payments": len(pos.payments),
        "next_payment": pos.next_payment.isoformat() if pos.next_payment else None,
        "matured": pos.matured,
    }
    asset.external_metadata = meta
    await session.flush()
    return pos


async def sync_all_fixed_income(session: AsyncSession) -> int:
    assets = (
        await session.execute(
            select(Asset).where(
                Asset.valuation_method == "fixed_income",
                Asset.is_archived.is_(False),
                Asset.sell_date.is_(None),
            )
        )
    ).scalars().all()
    done = 0
    for asset in assets:
        try:
            await sync_fixed_income_asset(session, asset)
            await session.commit()
            done += 1
        except Exception:
            await session.rollback()
            logger.exception("Fixed income sync failed for asset %s", asset.id)
    return done
