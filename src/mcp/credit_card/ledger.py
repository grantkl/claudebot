"""Mutable ledger tracking credit usage per period.

Stored as a JSON file in ``data/credit_card_ledger.json`` (configurable via
``CREDIT_CARD_LEDGER`` env var). Key operations:

* ``period_key(credit, today)`` — pure function that turns a credit's reset
  cadence into a unique bucket key (e.g. ``"2026-05"``, ``"2026-Q2"``,
  ``"cy:2025-05_2026-04"``). Unit-tested independently from I/O.
* ``mark_used(credit_id, amount, date, tx_id=None)`` — idempotent on
  ``tx_id``. Re-running an audit on the same Gmail message will not
  double-count usage.
* ``cap_remaining(credit, today)`` — how much credit is left in the current
  period. Returns 0 if the credit is sunset (past ``active_through``) or not
  yet active (before ``active_from``).
* ``record_switch_suggestion(merchant, card_id, today)`` /
  ``recently_suggested_switch(...)`` — 30-day cooldown so the audit doesn't
  nag every day about the same recurring subscription.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .catalog import Credit

SUGGESTION_COOLDOWN_DAYS = 30


def period_key(credit: Credit, today: date) -> str:
    """Return the canonical bucket key for ``credit`` on ``today``.

    The cadence determines the format:

    * ``monthly`` -> ``YYYY-MM``
    * ``quarterly`` -> ``YYYY-Q[1-4]`` (calendar quarter)
    * ``semi_annual`` -> ``YYYY-H[1-2]``
    * ``annual`` -> ``YYYY``
    * ``cardmember_year`` -> ``cy:YYYY-MM_YYYY-MM`` based on the
      ``cardmember_anchor_month``. The bucket starts on the anchor month and
      runs for 12 months.
    """

    p = credit.period
    if p == "monthly":
        return f"{today.year:04d}-{today.month:02d}"
    if p == "quarterly":
        q = (today.month - 1) // 3 + 1
        return f"{today.year:04d}-Q{q}"
    if p == "semi_annual":
        h = 1 if today.month <= 6 else 2
        return f"{today.year:04d}-H{h}"
    if p == "annual":
        return f"{today.year:04d}"
    if p == "cardmember_year":
        anchor = credit.cardmember_anchor_month
        if anchor is None:
            raise ValueError(
                f"credit {credit.id}: cardmember_year requires cardmember_anchor_month"
            )
        if today.month >= anchor:
            start_year = today.year
        else:
            start_year = today.year - 1
        end_year = start_year + 1
        end_month = anchor - 1
        if end_month == 0:
            end_month = 12
            end_year = end_year - 1
        return f"cy:{start_year:04d}-{anchor:02d}_{end_year:04d}-{end_month:02d}"
    raise ValueError(f"unknown period {p!r}")


def is_active(credit: Credit, today: date) -> bool:
    """Whether the credit is in its active window on ``today``."""

    if credit.active_from is not None and today < credit.active_from:
        return False
    if credit.active_through is not None and today > credit.active_through:
        return False
    return True


class CreditLedger:
    """JSON-backed credit usage ledger."""

    def __init__(self, file_path: str | os.PathLike[str]) -> None:
        self._path = Path(file_path)
        self._data: dict[str, Any] = {
            "version": 1,
            "credits": {},
            "suggested_switches": {},
        }
        self._load()

    # -------- I/O --------

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return
        self._data["credits"] = raw.get("credits", {}) or {}
        self._data["suggested_switches"] = raw.get("suggested_switches", {}) or {}
        self._data["version"] = raw.get("version", 1)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    # -------- Credit usage --------

    def used(self, credit: Credit, today: date) -> float:
        bucket = self._bucket(credit, today)
        return float(bucket.get("used", 0.0))

    def cap_remaining(self, credit: Credit, today: date) -> float:
        if not is_active(credit, today):
            return 0.0
        return max(0.0, credit.period_amount - self.used(credit, today))

    def mark_used(
        self,
        credit: Credit,
        amount: float,
        usage_date: date,
        tx_id: str | None = None,
    ) -> dict[str, Any]:
        """Record ``amount`` of credit usage. Idempotent on ``tx_id``.

        Returns the post-update ``{used, remaining}`` for the relevant period.
        """

        bucket = self._bucket(credit, usage_date)
        tx_ids: list[str] = bucket.setdefault("tx_ids", [])
        if tx_id is not None and tx_id in tx_ids:
            return {
                "used": float(bucket.get("used", 0.0)),
                "remaining": self.cap_remaining(credit, usage_date),
                "noop": True,
            }
        bucket["used"] = round(float(bucket.get("used", 0.0)) + float(amount), 2)
        if tx_id is not None:
            tx_ids.append(tx_id)
        self._save()
        return {
            "used": float(bucket["used"]),
            "remaining": self.cap_remaining(credit, usage_date),
            "noop": False,
        }

    def _bucket(self, credit: Credit, today: date) -> dict[str, Any]:
        credits_section: dict[str, Any] = self._data.setdefault("credits", {})
        per_credit: dict[str, Any] = credits_section.setdefault(credit.id, {})
        periods: dict[str, Any] = per_credit.setdefault("periods", {})
        key = period_key(credit, today)
        return periods.setdefault(key, {"used": 0.0, "tx_ids": []})

    # -------- Switch-suggestion cooldown --------

    @staticmethod
    def _switch_key(merchant_normalized: str, card_id: str) -> str:
        return f"{merchant_normalized}|{card_id}"

    def recently_suggested_switch(
        self, merchant_normalized: str, card_id: str, today: date,
        cooldown_days: int = SUGGESTION_COOLDOWN_DAYS,
    ) -> bool:
        suggestions: dict[str, str] = self._data.get("suggested_switches", {}) or {}
        ts = suggestions.get(self._switch_key(merchant_normalized, card_id))
        if not ts:
            return False
        try:
            suggested_at = datetime.fromisoformat(ts).date()
        except ValueError:
            return False
        return (today - suggested_at) < timedelta(days=cooldown_days)

    def record_switch_suggestion(
        self, merchant_normalized: str, card_id: str, today: date
    ) -> None:
        suggestions: dict[str, str] = self._data.setdefault("suggested_switches", {})
        suggestions[self._switch_key(merchant_normalized, card_id)] = datetime.combine(
            today, datetime.min.time(), tzinfo=timezone.utc
        ).isoformat()
        self._save()

    # -------- Inspection --------

    def snapshot(self) -> dict[str, Any]:
        """Return a deep-ish copy of the ledger state for read-only use."""

        return json.loads(json.dumps(self._data))


# --------------------------------------------------------------------------- #
# Module-level singleton (mirrors shopping_list_server convention).
# --------------------------------------------------------------------------- #

_DEFAULT_LEDGER_PATH = "data/credit_card_ledger.json"

_ledger: CreditLedger | None = None


def get_ledger() -> CreditLedger:
    global _ledger
    if _ledger is None:
        path = os.environ.get("CREDIT_CARD_LEDGER", _DEFAULT_LEDGER_PATH)
        _ledger = CreditLedger(path)
    return _ledger


def reset_singleton() -> None:
    """Reset the module-level singleton. Used by tests."""

    global _ledger
    _ledger = None
