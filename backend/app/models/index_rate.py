from datetime import date as _date
from decimal import Decimal

from sqlalchemy import Date, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class IndexRate(Base):
    """Published Brazilian index values from the BCB (fixed income).

    series "cdi": % per business day (SGS 12), keyed by day.
    series "ipca": % per month (SGS 433), keyed by the first day of the month.
    """

    __tablename__ = "index_rates"

    series: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[_date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(precision=14, scale=8))
