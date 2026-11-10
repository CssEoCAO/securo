"""Fixed-income engine (renda fixa): pure math, no database.

Models a position bought for ``principal`` on ``start`` that earns:

- ``prefixado``: ``rate`` % per year, compounded daily over calendar days
  (``(1 + rate)^(days/365)``);
- ``cdi``: ``rate`` % of the CDI, compounding each published daily CDI rate
  (BCB SGS series 12, % per business day);
- ``ipca``: IPCA (BCB SGS series 433, % per month, pro-rated by calendar days
  inside a month) times ``rate`` % per year on top.

Income is either paid once at ``maturity`` (compound: principal and income
come back together) or periodically (``monthly``, ``quarterly``,
``semiannual``, ``yearly``: each coupon pays the income accrued since the
previous one; the principal comes back at maturity).

Income tax (IR) is withheld on income only:
``regressiva`` (22.5% up to 180 days, 20% up to 360, 17.5% up to 720, 15%
after, counted from ``start`` to the payment), ``isento`` (LCI, LCA,
incentivised debentures) or ``fixa`` (``tax_rate`` %).

Simplifications, stated: prefixado and the IPCA spread use calendar days
(not the 252 business-day convention), IOF on redemptions within 30 days is
not modelled, and days after the last published CDI reuse the last value on
weekdays.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

INDEXES = ("prefixado", "cdi", "ipca")
PAYMENTS = ("maturity", "monthly", "quarterly", "semiannual", "yearly")
TAXES = ("regressiva", "isento", "fixa")
_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "yearly": 12}
CENT = Decimal("0.01")


def money(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def ir_rate(days: int, tax: str, tax_rate: Optional[Decimal] = None) -> Decimal:
    if tax == "isento":
        return Decimal(0)
    if tax == "fixa":
        return (tax_rate or Decimal(0)) / 100
    if days <= 180:
        return Decimal("0.225")
    if days <= 360:
        return Decimal("0.20")
    if days <= 720:
        return Decimal("0.175")
    return Decimal("0.15")


@dataclass
class Params:
    principal: Decimal
    start: date
    index: str = "prefixado"
    rate: Decimal = Decimal(0)
    payment: str = "maturity"
    maturity: Optional[date] = None
    tax: str = "regressiva"
    tax_rate: Optional[Decimal] = None

    def validate(self) -> None:
        if self.index not in INDEXES:
            raise ValueError(f"índice inválido: {self.index}")
        if self.payment not in PAYMENTS:
            raise ValueError(f"pagamento inválido: {self.payment}")
        if self.tax not in TAXES:
            raise ValueError(f"IR inválido: {self.tax}")
        if self.principal is None or self.principal <= 0:
            raise ValueError("valor aplicado precisa ser maior que zero")
        if self.maturity is not None and self.maturity <= self.start:
            raise ValueError("vencimento precisa ser depois da aplicação")
        if self.payment != "maturity" and self.maturity is None:
            raise ValueError("pagamento periódico precisa de vencimento")
        if self.tax == "fixa" and self.tax_rate is None:
            raise ValueError("IR fixo precisa da alíquota")


@dataclass
class Indexes:
    """Published index values. ``cdi``: date → % per day. ``ipca``:
    (year, month) → % per month."""
    cdi: dict[date, Decimal] = field(default_factory=dict)
    ipca: dict[tuple[int, int], Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._cdi_dates = sorted(self.cdi)

    def cdi_on(self, d: date) -> Optional[Decimal]:
        if d in self.cdi:
            return self.cdi[d]
        if self._cdi_dates and d > self._cdi_dates[-1] and d.weekday() < 5:
            return self.cdi[self._cdi_dates[-1]]
        return None

    def ipca_month(self, year: int, month: int) -> Decimal:
        if (year, month) in self.ipca:
            return self.ipca[(year, month)]
        known = [k for k in self.ipca if k <= (year, month)]
        return self.ipca[max(known)] if known else Decimal(0)


def factor(p: Params, ix: Indexes, a: date, b: date) -> Decimal:
    """Growth of 1 between ``a`` (exclusive) and ``b`` (inclusive)."""
    if b <= a:
        return Decimal(1)
    days = (b - a).days
    if p.index == "prefixado":
        return Decimal(str((1 + float(p.rate) / 100) ** (days / 365)))
    if p.index == "cdi":
        f = Decimal(1)
        pct = p.rate / 100
        d = a + timedelta(days=1)
        while d <= b:
            v = ix.cdi_on(d)
            if v is not None:
                f *= 1 + v / 100 * pct
            d += timedelta(days=1)
        return f
    # ipca + spread
    f = Decimal(str((1 + float(p.rate) / 100) ** (days / 365)))
    cursor = a
    while cursor < b:
        first = cursor + timedelta(days=1)   # days in (cursor, seg_end] share this month
        y, m = first.year, first.month
        month_days = calendar.monthrange(y, m)[1]
        month_end = date(y, m, month_days)
        seg_end = min(b, month_end)
        frac = (seg_end - cursor).days / month_days
        f *= Decimal(str((1 + float(ix.ipca_month(y, m)) / 100) ** frac))
        cursor = seg_end
    return f


@dataclass
class Payment:
    date: date
    kind: str              # "cupom" | "vencimento"
    gross_income: Decimal
    ir: Decimal
    ir_rate: Decimal
    principal: Decimal     # principal returned with this payment

    @property
    def net(self) -> Decimal:
        return money(self.gross_income - self.ir + self.principal)


@dataclass
class Position:
    as_of: date
    gross_value: Decimal
    ir: Decimal
    ir_rate: Decimal
    matured: bool
    payments: list[Payment]
    next_payment: Optional[date]

    @property
    def net_value(self) -> Decimal:
        return money(self.gross_value - self.ir)

    @property
    def paid_net(self) -> Decimal:
        return money(sum((x.net for x in self.payments), Decimal(0)))


def coupon_dates(p: Params) -> list[date]:
    if p.payment == "maturity":
        return [p.maturity] if p.maturity else []
    step = _MONTHS[p.payment]
    out, k = [], 1
    while True:
        d = add_months(p.start, step * k)
        if p.maturity and d >= p.maturity:
            out.append(p.maturity)
            return out
        out.append(d)
        k += 1
        if k > 1200:
            return out


def position(p: Params, ix: Indexes, today: date) -> Position:
    p.validate()
    end = min(today, p.maturity) if p.maturity else today
    matured = p.maturity is not None and today >= p.maturity
    payments: list[Payment] = []
    last = p.start
    for d in coupon_dates(p):
        if d > end:
            break
        income = p.principal * (factor(p, ix, last, d) - 1)
        rate = ir_rate((d - p.start).days, p.tax, p.tax_rate)
        payments.append(Payment(
            date=d,
            kind="vencimento" if p.maturity and d == p.maturity else "cupom",
            gross_income=money(income),
            ir=money(income * rate),
            ir_rate=rate,
            principal=p.principal if p.maturity and d == p.maturity else Decimal(0),
        ))
        last = d
    if matured:
        return Position(today, Decimal(0), Decimal(0), Decimal(0), True, payments, None)
    accrued = p.principal * (factor(p, ix, last, end) - 1)
    rate = ir_rate((end - p.start).days, p.tax, p.tax_rate)
    nxt = next((d for d in coupon_dates(p) if d > end), None)
    return Position(end, money(p.principal + accrued), money(accrued * rate), rate, False, payments, nxt)


def value_points(p: Params, ix: Indexes, today: date) -> list[tuple[date, Decimal]]:
    """Net value at each month end from start to today (or maturity), plus
    today. For the chart."""
    end = min(today, p.maturity) if p.maturity else today
    points: list[date] = [p.start]
    d = date(p.start.year, p.start.month, calendar.monthrange(p.start.year, p.start.month)[1])
    while d < end:
        if d > p.start:
            points.append(d)
        nxt = add_months(date(d.year, d.month, 1), 1)
        d = date(nxt.year, nxt.month, calendar.monthrange(nxt.year, nxt.month)[1])
    if end not in points:
        points.append(end)
    out = []
    for d in points:
        if p.maturity and d >= p.maturity:
            break
        out.append((d, position(p, ix, d).net_value))
    return out


def needed_series(p: Params) -> Iterable[str]:
    return {"cdi": ["cdi"], "ipca": ["ipca"]}.get(p.index, [])
