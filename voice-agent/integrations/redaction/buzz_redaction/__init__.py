"""Regex redaction of sensitive data in call transcripts (PLAN section 7).

Runs on every transcript and summary before anything leaves the app server
for HubSpot or Google Sheets. Pure functions, no I/O.

What is redacted, in this order:

1. Zambian NRC numbers, ``######/##/#`` (spaces around the slashes allowed).
2. Payment card numbers: 13-19 digits, optionally grouped with spaces or
   dashes, **only if the Luhn check passes**. A non-Luhn digit run is left for
   the phone rule (if enabled) so that order numbers are not mangled.
3. PINs and one-time codes: 3-8 digits, or 3-8 spoken digit words, appearing
   within a few words after a trigger word (PIN, passcode, password, OTP,
   CVV, security code, verification code ...).
4. Passport-like IDs: one or two letters followed by 6-8 digits (Zambian
   passports are ``ZN`` plus 6 digits). A single ``K`` prefix is excluded so
   kwacha amounts such as ``K150000`` survive.
5. Phone numbers, only with ``phones=True``: 9-15 digits, optional leading
   ``+``, separated by spaces, dashes, dots or brackets.

Things deliberately left alone (see the false-positive tests): dates
(``19/10/2026``, ``2026-10-19``), times (``10:30``), kwacha amounts
(``K1,500``, ``ZMW 2 500``), years, short counts.

Redaction is a safety net, not the primary control: the agent prompt already
forbids asking for these data (PLAN section 7). It is biased towards
redacting when in doubt.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RedactionResult", "redact", "redact_text", "luhn_valid", "PLACEHOLDERS"]

PLACEHOLDERS = {
    "nrc": "[REDACTED:NRC]",
    "card": "[REDACTED:CARD]",
    "pin": "[REDACTED:PIN]",
    "id": "[REDACTED:ID]",
    "phone": "[REDACTED:PHONE]",
}


@dataclass
class RedactionResult:
    text: str
    counts: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def luhn_valid(digits: str) -> bool:
    if not digits.isascii() or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_NRC = re.compile(r"(?<![\d/])\d{6}\s?/\s?\d{2}\s?/\s?\d(?![\d/])")

_CARD = re.compile(r"(?<![\d+])\d(?:[ \-]?\d){12,18}(?![\d])")

_DIGIT_WORD = r"(?:zero|oh|nought|one|two|three|four|five|six|seven|eight|nine)"
_TRIGGER = (
    r"(?:pin(?:\s+(?:number|code))?|passcode|pass\s?word|password|otp|"
    r"one[\s-]time\s+(?:pin|password|code)|cvv2?|cvc|security\s+code|"
    r"verification\s+code|access\s+code|secret\s+code)"
)
# Trigger, then up to 30 non-digit characters (e.g. " is ", " number is: "),
# then the code itself as digits or spoken digit words.
_PIN = re.compile(
    rf"(?P<trigger>\b{_TRIGGER}\b)"
    rf"(?P<gap>[^\d\n]{{0,30}}?)"
    rf"(?P<code>(?<!\d)\d(?:[ \-]?\d){{2,7}}(?!\d)"
    rf"|\b{_DIGIT_WORD}(?:[\s,\-]+{_DIGIT_WORD}){{2,7}}\b)",
    re.IGNORECASE,
)

_PASSPORT = re.compile(r"\b(?!K\d)[A-Z]{1,2}\d{6,8}\b", re.IGNORECASE)

_PHONE = re.compile(r"(?<![\w+(])(?:\+|\(|\+\()?\d[\d \-.()]{7,20}\d(?!\w)")


def _sub_nrc(text: str, counts: Counter) -> str:
    def repl(m: re.Match) -> str:
        counts["nrc"] += 1
        return PLACEHOLDERS["nrc"]

    return _NRC.sub(repl, text)


def _sub_card(text: str, counts: Counter) -> str:
    def repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            counts["card"] += 1
            return PLACEHOLDERS["card"]
        return m.group(0)

    return _CARD.sub(repl, text)


def _sub_pin(text: str, counts: Counter) -> str:
    def repl(m: re.Match) -> str:
        counts["pin"] += 1
        return f"{m.group('trigger')}{m.group('gap')}{PLACEHOLDERS['pin']}"

    return _PIN.sub(repl, text)


def _sub_passport(text: str, counts: Counter) -> str:
    def repl(m: re.Match) -> str:
        counts["id"] += 1
        return PLACEHOLDERS["id"]

    return _PASSPORT.sub(repl, text)


def _sub_phone(text: str, counts: Counter) -> str:
    def repl(m: re.Match) -> str:
        s = m.group(0)
        n = sum(ch.isdigit() for ch in s)
        if 9 <= n <= 15:
            counts["phone"] += 1
            return PLACEHOLDERS["phone"]
        return s

    return _PHONE.sub(repl, text)


def redact_text(text: str, *, phones: bool = False) -> RedactionResult:
    """Redact one string. Returns the new text and counts per kind."""
    counts: Counter = Counter()
    if not text:
        return RedactionResult(text or "", counts)
    out = _sub_nrc(text, counts)
    out = _sub_card(out, counts)
    out = _sub_pin(out, counts)
    out = _sub_passport(out, counts)
    if phones:
        out = _sub_phone(out, counts)
    return RedactionResult(out, counts)


def redact(value: Any, *, phones: bool = False, counts: Counter | None = None) -> Any:
    """Redact every string inside a JSON-like value (str, list, dict).

    Dict keys are not touched. Returns a new value; ``counts`` (if given) is
    updated in place.
    """
    if counts is None:
        counts = Counter()
    if isinstance(value, str):
        r = redact_text(value, phones=phones)
        counts.update(r.counts)
        return r.text
    if isinstance(value, list):
        return [redact(v, phones=phones, counts=counts) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, phones=phones, counts=counts) for k, v in value.items()}
    return value
