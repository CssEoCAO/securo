"""Mercado Pago connector (paste-a-token flow).

Reads the account's money movements from Mercado Pago's own API, without an
open-finance aggregator. The user pastes a production Access Token from
https://www.mercadopago.com.br/developers/panel together with the account's
current balance ("APP_USR-... 1234,56").

Movements come from the *settlement* ("dinheiro em conta") report: the API
generates it asynchronously for a date window (whole days, at most 60), and
we poll until the CSV is ready. Rows carry date, type, amount and a few tags
but no description, so one is built from the type and, for purchases, the
Payments API.

Mercado Pago exposes no balance endpoint to third-party apps, so the balance
is the one the user typed at connect time plus every movement settled after
that moment. Movements are counted once each (their ids are kept for a few
weeks in the credentials), which also catches rows the report lists late.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx

from app.agents.services.crypto import decrypt, encrypt
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    ProviderUserActionRequired,
    SessionExpiredError,
    TransactionData,
)

logger = logging.getLogger(__name__)

API = "https://api.mercadopago.com"
PANEL_URL = "https://www.mercadopago.com.br/developers/panel"
REPORT_COLUMNS = [
    "TRANSACTION_DATE", "SOURCE_ID", "EXTERNAL_REFERENCE", "TRANSACTION_TYPE",
    "TRANSACTION_AMOUNT", "TRANSACTION_CURRENCY", "SETTLEMENT_NET_AMOUNT",
    "SETTLEMENT_DATE", "FEE_AMOUNT", "PAYMENT_METHOD_TYPE", "PAYMENT_METHOD",
    "DESCRIPTION", "OPERATION_TAGS", "ORDER_ID",
]
INITIAL_HISTORY_DAYS = 60       # one report on connect
RECOUNT_WINDOW_DAYS = 15        # late rows are still counted within this window
REPORT_POLL_SECONDS = 10
REPORT_TIMEOUT_SECONDS = 240
ENRICH_LIMIT = 60               # Payments API lookups per sync
COUNTED_KEEP_DAYS = 30

_TOKEN_RE = re.compile(r"(APP_USR-[A-Za-z0-9-]+|TEST-[A-Za-z0-9-]+)")


def parse_paste(text: str) -> tuple[str, Optional[Decimal]]:
    """Split the pasted text into (token, balance). Balance is optional here;
    the connect step requires it. Accepts "1.234,56", "1234,56", "1234.56"."""
    text = (text or "").strip()
    m = _TOKEN_RE.search(text)
    if not m:
        return "", None
    rest = (text[: m.start()] + " " + text[m.end():]).strip()
    rest = re.sub(r"(?i)saldo\s*[:=]?\s*|r\$\s*", "", rest).strip(" ;|")
    if not rest:
        return m.group(1), None
    raw = rest.replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return m.group(1), Decimal(raw)
    except InvalidOperation:
        return m.group(1), None


def _dec(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").strip() or "0")
    except InvalidOperation:
        return Decimal(0)


def _parse_ts(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone(timedelta(hours=-3)))
    return ts


def row_id(row: dict) -> str:
    key = "|".join(row.get(k, "") for k in ("SOURCE_ID", "TRANSACTION_TYPE", "TRANSACTION_DATE", "TRANSACTION_AMOUNT"))
    return "mp:" + hashlib.sha256(key.encode()).hexdigest()[:24]


def row_amount(row: dict) -> Decimal:
    net = (row.get("SETTLEMENT_NET_AMOUNT") or "").strip()
    return _dec(net) if net else _dec(row.get("TRANSACTION_AMOUNT"))


_TYPE_LABEL = {
    "PAYOUTS": "Saque / transferência",
    "CASHBACK": "Cashback",
    "REFUND": "Estorno",
    "CHARGEBACK": "Contestação",
    "DISPUTE": "Disputa",
    "WITHDRAWAL": "Saque",
    "WITHDRAWAL_CANCEL": "Saque cancelado",
}
_METHOD_LABEL = {
    "pix": "Pix", "account_money": "saldo Mercado Pago", "bolbradesco": "boleto",
    "pec": "boleto", "debit_card": "cartão de débito", "credit_card": "cartão de crédito",
    "bank_transfer": "transferência",
}


def row_description(row: dict) -> str:
    tags = row.get("OPERATION_TAGS") or ""
    amount = row_amount(row)
    kind = (row.get("TRANSACTION_TYPE") or "").upper()
    method = (row.get("PAYMENT_METHOD") or row.get("PAYMENT_METHOD_TYPE") or "").lower()
    if kind in _TYPE_LABEL:
        label = _TYPE_LABEL[kind]
    elif "PIX" in tags.upper() or method == "pix":
        label = "Pix recebido" if amount >= 0 else "Pix enviado"
    else:
        label = "Recebimento" if amount >= 0 else "Pagamento"
        if method in _METHOD_LABEL:
            label += f" ({_METHOD_LABEL[method]})"
    if "Utility_ID" in tags:
        label = "Pagamento de conta" if amount < 0 else label
    extra = (row.get("DESCRIPTION") or "").strip()
    return f"{label} · {extra}" if extra else label


class MercadoPagoProvider(BankProvider):
    def __init__(self) -> None:
        self._rows_cache: dict[tuple[str, str], list[dict]] = {}
        self._payment_cache: dict[str, Optional[str]] = {}

    @property
    def name(self) -> str:
        return "mercadopago"

    @property
    def flow_type(self) -> str:
        return "token"

    # ----- credentials -------------------------------------------------------

    @staticmethod
    def _token(credentials: dict) -> str:
        enc = (credentials or {}).get("access_token_enc")
        token = decrypt(enc) if enc else None
        token = token or (credentials or {}).get("access_token") or ""
        if not token:
            raise SessionExpiredError("Mercado Pago access token is missing")
        return token

    async def _request(self, method: str, path: str, token: str, **kw) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        if resp.status_code == 401:
            raise ProviderUserActionRequired(
                "Mercado Pago recusou o token (401). Gere um Access Token de produção novo e reconecte.",
                code="credentials_invalid",
                help_url=PANEL_URL,
            )
        return resp

    async def _ensure_report_config(self, token: str) -> None:
        resp = await self._request("GET", "/v1/account/settlement_report/config", token)
        body = {
            "file_name_prefix": "securo-settlement",
            "include_withdraw": True,
            "refund_detailed": True,
            "display_timezone": "GMT-03",
            "header_language": "en",
            "frequency": {"hour": 0, "type": "daily", "value": 1},
            "columns": [{"key": k} for k in REPORT_COLUMNS],
        }
        if resp.status_code == 404:
            r = await self._request("POST", "/v1/account/settlement_report/config", token, json=body)
        else:
            resp.raise_for_status()
            have = {c.get("key") for c in resp.json().get("columns", [])}
            if set(REPORT_COLUMNS) <= have:
                return
            r = await self._request("PUT", "/v1/account/settlement_report/config", token, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Mercado Pago report config failed ({r.status_code}): {r.text[:200]}")

    # ----- report ------------------------------------------------------------

    async def _report_rows(self, token: str, begin: date, end: date) -> list[dict]:
        """Rows of the settlement report for whole days [begin, end] (BRT)."""
        key = (begin.isoformat(), end.isoformat())
        if key in self._rows_cache:
            return self._rows_cache[key]
        brt = timezone(timedelta(hours=-3))
        b = datetime.combine(begin, datetime.min.time(), brt).astimezone(timezone.utc)
        e = datetime.combine(end + timedelta(days=1), datetime.min.time(), brt).astimezone(timezone.utc) - timedelta(seconds=1)
        resp = await self._request(
            "POST", "/v1/account/settlement_report", token,
            json={"begin_date": b.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_date": e.strftime("%Y-%m-%dT%H:%M:%SZ")},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Mercado Pago report request failed ({resp.status_code}): {resp.text[:200]}")
        report_id = (resp.json() or {}).get("id")
        file_name = None
        waited = 0
        while waited <= REPORT_TIMEOUT_SECONDS:
            listing = await self._request("GET", "/v1/account/settlement_report/list", token)
            listing.raise_for_status()
            for item in listing.json() or []:
                if item.get("id") == report_id and item.get("file_name") and item.get("status") in (None, "processed", "ready"):
                    file_name = item["file_name"]
            if file_name:
                break
            await asyncio.sleep(REPORT_POLL_SECONDS)
            waited += REPORT_POLL_SECONDS
        if not file_name:
            raise RuntimeError("Mercado Pago report was not ready in time; the next sync retries")
        data = await self._request("GET", f"/v1/account/settlement_report/{file_name}", token)
        data.raise_for_status()
        text = data.content.decode("utf-8-sig", errors="replace")
        first = text.splitlines()[0] if text else ""
        delim = ";" if first.count(";") > first.count(",") else ","
        rows = [r for r in csv.DictReader(io.StringIO(text), delimiter=delim) if r.get("TRANSACTION_DATE")]
        self._rows_cache[key] = rows
        return rows

    async def _payment_description(self, token: str, source_id: str) -> Optional[str]:
        if not source_id or source_id in self._payment_cache:
            return self._payment_cache.get(source_id)
        desc = None
        try:
            resp = await self._request("GET", f"/v1/payments/{source_id}", token)
            if resp.status_code == 200:
                p = resp.json()
                desc = (p.get("description") or p.get("statement_descriptor") or "").strip() or None
        except ProviderUserActionRequired:
            raise
        except Exception:
            desc = None
        self._payment_cache[source_id] = desc
        return desc

    # ----- connection flow ---------------------------------------------------

    def get_oauth_url(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError("Mercado Pago uses paste-a-token flow, not OAuth redirect")

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        token, balance = parse_paste(code)
        if not token:
            raise ProviderUserActionRequired(
                "Cole o Access Token de produção (começa com APP_USR-) seguido do saldo atual. Ex.: APP_USR-... 1234,56",
                code="token_invalid", help_url=PANEL_URL,
            )
        if balance is None:
            raise ProviderUserActionRequired(
                "Faltou o saldo atual da conta: a API do Mercado Pago não informa saldo. "
                "Cole o token e o saldo separados por espaço. Ex.: APP_USR-... 1234,56",
                code="balance_missing", help_url=PANEL_URL,
            )
        me = await self._request("GET", "/users/me", token)
        me.raise_for_status()
        user = me.json()
        await self._ensure_report_config(token)
        now = datetime.now(timezone.utc)
        nickname = user.get("nickname") or str(user.get("id"))
        credentials = {
            "access_token_enc": encrypt(token) or token,
            "user_id": str(user.get("id")),
            "nickname": nickname,
            "connected_at": now.isoformat(),
            "balance": str(balance),
            "counted": {},
        }
        return ConnectionData(
            external_id=f"mp-{user.get('id')}",
            institution_name="Mercado Pago",
            credentials=credentials,
            accounts=[self._account(credentials)],
            logo_url=None,
        )

    def _account(self, credentials: dict) -> AccountData:
        return AccountData(
            external_id=f"mp-{credentials.get('user_id')}",
            name="Mercado Pago",
            type="checking",
            balance=_dec(credentials.get("balance")),
            currency="BRL",
        )

    async def refresh_credentials(self, credentials: dict) -> dict:
        """Roll the balance forward with movements settled since connect that
        were not counted yet."""
        token = self._token(credentials)
        connected_at = _parse_ts(credentials.get("connected_at", "")) or datetime.now(timezone.utc)
        today = datetime.now(timezone(timedelta(hours=-3))).date()
        begin = max(connected_at.astimezone(timezone(timedelta(hours=-3))).date(), today - timedelta(days=RECOUNT_WINDOW_DAYS))
        rows = await self._report_rows(token, begin, today)
        counted: dict[str, str] = dict(credentials.get("counted") or {})
        balance = _dec(credentials.get("balance"))
        for row in rows:
            ts = _parse_ts(row.get("TRANSACTION_DATE", ""))
            rid = row_id(row)
            if ts is None or ts <= connected_at or rid in counted:
                continue
            balance += row_amount(row)
            counted[rid] = ts.date().isoformat()
        cutoff = (today - timedelta(days=COUNTED_KEEP_DAYS)).isoformat()
        counted = {k: v for k, v in counted.items() if v >= cutoff}
        return {**credentials, "balance": str(balance), "counted": counted}

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        return [self._account(credentials)]

    async def get_transactions(
        self, credentials: dict, account_external_id: str,
        since: Optional[date] = None, payee_source: str = "auto",
    ) -> list[TransactionData]:
        token = self._token(credentials)
        today = datetime.now(timezone(timedelta(hours=-3))).date()
        begin = since if since else today - timedelta(days=INITIAL_HISTORY_DAYS - 1)
        begin = max(begin, today - timedelta(days=INITIAL_HISTORY_DAYS - 1))
        # reuse the refresh window when it already covers this one
        rows: list[dict] = []
        for (b, e), cached in self._rows_cache.items():
            if b <= begin.isoformat() and e >= today.isoformat():
                rows = [r for r in cached if (_parse_ts(r["TRANSACTION_DATE"]) or datetime.min.replace(tzinfo=timezone.utc)).date() >= begin]
                break
        else:
            rows = await self._report_rows(token, begin, today)

        out: list[TransactionData] = []
        lookups = 0
        for row in rows:
            ts = _parse_ts(row.get("TRANSACTION_DATE", ""))
            if ts is None:
                continue
            amount = row_amount(row)
            if amount == 0:
                continue
            description = row_description(row)
            kind = (row.get("TRANSACTION_TYPE") or "").upper()
            if kind == "SETTLEMENT" and lookups < ENRICH_LIMIT and "PIX" not in (row.get("OPERATION_TAGS") or "").upper():
                lookups += 1
                extra = await self._payment_description(token, row.get("SOURCE_ID", ""))
                if extra:
                    description = f"{description} · {extra}"
            out.append(TransactionData(
                external_id=row_id(row),
                description=description[:500],
                amount=abs(amount),
                date=ts.astimezone(timezone(timedelta(hours=-3))).date(),
                type="credit" if amount >= 0 else "debit",
                currency=(row.get("TRANSACTION_CURRENCY") or "BRL")[:3] or "BRL",
                status="posted",
                payee=None,
                raw_data={k: row.get(k) for k in ("SOURCE_ID", "TRANSACTION_TYPE", "PAYMENT_METHOD", "OPERATION_TAGS", "ORDER_ID")},
            ))
        return out
