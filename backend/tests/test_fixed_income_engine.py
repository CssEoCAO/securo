"""Fixed-income engine: rates, IR table, coupons and maturity."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.fixed_income import (
    Indexes,
    Params,
    add_months,
    coupon_dates,
    factor,
    ir_rate,
    position,
    value_points,
)

D = Decimal


def test_ir_regressiva_brackets():
    assert ir_rate(180, "regressiva") == D("0.225")
    assert ir_rate(181, "regressiva") == D("0.20")
    assert ir_rate(360, "regressiva") == D("0.20")
    assert ir_rate(720, "regressiva") == D("0.175")
    assert ir_rate(721, "regressiva") == D("0.15")
    assert ir_rate(10, "isento") == 0
    assert ir_rate(10, "fixa", D("15")) == D("0.15")


def test_add_months_clamps_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_prefixado_compound_one_year():
    p = Params(principal=D("1000"), start=date(2025, 1, 1), rate=D("25"), maturity=date(2026, 1, 1))
    assert factor(p, Indexes(), p.start, date(2026, 1, 1)).quantize(D("0.0001")) == D("1.2500")


def test_compound_paid_at_maturity_with_ir():
    p = Params(principal=D("10000"), start=date(2024, 1, 1), rate=D("12"),
               maturity=date(2026, 1, 1), tax="regressiva")
    pos = position(p, Indexes(), date(2026, 6, 1))
    assert pos.matured and len(pos.payments) == 1
    pay = pos.payments[0]
    assert pay.kind == "vencimento" and pay.principal == D("10000")
    # 731 days -> 15% IR; 12% a.a. over 731 days
    income = D(str(10000 * (1.12 ** (731 / 365) - 1))).quantize(D("0.01"))
    assert pay.gross_income == income
    assert pay.ir == (income * D("0.15")).quantize(D("0.01"))
    assert pay.net == (D("10000") + income - pay.ir).quantize(D("0.01"))


def test_monthly_coupons_like_allu():
    # 25% a.a. paid monthly, principal back at maturity, IR regressiva
    p = Params(principal=D("10000"), start=date(2026, 1, 10), rate=D("25"),
               payment="monthly", maturity=date(2027, 1, 10))
    assert len(coupon_dates(p)) == 12 and coupon_dates(p)[-1] == date(2027, 1, 10)
    pos = position(p, Indexes(), date(2026, 4, 20))
    assert [x.date for x in pos.payments] == [date(2026, 2, 10), date(2026, 3, 10), date(2026, 4, 10)]
    first = pos.payments[0]
    expected = D(str(10000 * (1.25 ** (31 / 365) - 1))).quantize(D("0.01"))
    assert first.gross_income == expected and first.ir_rate == D("0.225") and first.principal == 0
    # value = principal + income accrued since the last coupon (10 days)
    accrued = D(str(10000 * (1.25 ** (10 / 365) - 1))).quantize(D("0.01"))
    assert pos.gross_value == D("10000") + accrued
    assert pos.next_payment == date(2026, 5, 10)
    final = position(p, Indexes(), date(2027, 2, 1))
    assert final.matured and final.payments[-1].principal == D("10000")


def test_cdi_percentage_compounds_daily_rates():
    start = date(2026, 3, 2)  # Monday
    cdi = {start + timedelta(days=i): D("0.05") for i in range(1, 8) if (start + timedelta(days=i)).weekday() < 5}
    p = Params(principal=D("1000"), start=start, index="cdi", rate=D("110"))
    f = factor(p, Indexes(cdi=cdi), start, start + timedelta(days=7))
    assert f == (1 + D("0.05") / 100 * D("1.10")) ** len(cdi)


def test_ipca_plus_spread():
    p = Params(principal=D("1000"), start=date(2026, 1, 1), index="ipca", rate=D("6"))
    ix = Indexes(ipca={(2026, 1): D("0.5")})
    f = factor(p, ix, date(2026, 1, 1), date(2026, 2, 1))
    expected = D(str((1.06 ** (31 / 365)))) * D(str(1.005 ** (30 / 31))) * D(str(1.005 ** (1 / 28)))
    assert abs(f - expected) < D("0.000001")


def test_value_points_are_net_and_monthly():
    p = Params(principal=D("1000"), start=date(2026, 1, 15), rate=D("10"), tax="isento")
    pts = value_points(p, Indexes(), date(2026, 4, 2))
    assert [d for d, _ in pts] == [date(2026, 1, 15), date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 2)]
    assert pts[0][1] == D("1000.00") and pts[-1][1] > pts[-2][1]


@pytest.mark.parametrize("bad", [
    dict(principal=D("0")),
    dict(payment="monthly", maturity=None),
    dict(maturity=date(2020, 1, 1)),
    dict(tax="fixa", tax_rate=None),
    dict(index="selic"),
])
def test_validation(bad):
    base = dict(principal=D("100"), start=date(2025, 1, 1), rate=D("10"))
    with pytest.raises(ValueError):
        position(Params(**{**base, **bad}), Indexes(), date(2026, 1, 1))
