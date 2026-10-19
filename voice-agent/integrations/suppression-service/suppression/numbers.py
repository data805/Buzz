"""Zambian number rules: normalisation to E.164 and classification.

Pure functions only (no I/O), so that every rule is unit-tested.

Sources for the ranges below (checked 19 October 2026):

* ITU-T E.164 national numbering plan for Zambia (+260), ZICTA communication of
  24 December 2024, published by ITU on 28 February 2025:
  https://www.itu.int/dms_pub/itu-t/oth/02/02/T02020000E80007PDFE.pdf
  (index: https://www.itu.int/oth/T02020000E8/en). This is the authoritative
  table. It says the NSN is always exactly 9 digits and lists:
    - 211-218 geographic (Zamtel): 211 Lusaka Province, 212 Copperbelt,
      213 Livingstone, 214 Kasama, 215 Kabwe, 216 Chipata, 217 Solwezi, 218 Mongu.
      20, 210 reserved; 219, 22-29 free.
    - Mobile assigned: 95, 75 Zamtel; 96, 76 MTN; 97, 77, 57 Airtel (57 live from
      15:00 CAT 23/12/2024); 98 Beeline (now ZedMobile).
      Reserved, not yet assigned: 55 Zamtel, 56 MTN, 58 and 78 Beeline.
      Free: 50-54, 59, 70-74, 79, 91-94, 99.
    - 63 VoIP, assigned to Liquid Telecom Zambia; 60-62, 64-69 VoIP free.
    - 8 special services (free-phone, local-rate numbers); 90 premium rate.
    - 1, 3, 4 reserved for future services.
* Google libphonenumber metadata, territory ZM (PhoneNumberMetadata.xml and
  ShortNumberMetadata.xml, master as of 7 October 2026):
  https://github.com/google/libphonenumber/blob/master/resources/PhoneNumberMetadata.xml
  Agrees on 9-digit NSNs, fixed line 21[1-8], toll free 800, VoIP 63. Its mobile
  pattern (?:[59][5-8]|7[5-9]) is looser than the ITU table (it also accepts the
  reserved 55, 56, 58, 78 and the free 79); we follow the ITU table.
  Short numbers are 3 digits: 112, 991, 993, 999.

Policy (fail closed): only *assigned* subscriber ranges are callable. A
reserved or free range returns ``invalid`` until this file is updated, so when
ZICTA assigns a new range (for example 55 or 56) a developer must add it here,
with its source, and the tests must be updated. That is deliberate.

Mobile number portability means the prefix does not reliably identify the
operator; we never use the operator for anything except documentation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

COUNTRY_CODE = "260"
NSN_LENGTH = 9
MAX_RAW_LENGTH = 64

# Two-digit NDCs assigned to mobile operators (ITU/ZICTA 24.XII.2024).
MOBILE_NDCS: dict[str, str] = {
    "95": "Zamtel",
    "75": "Zamtel",
    "96": "MTN",
    "76": "MTN",
    "97": "Airtel",
    "77": "Airtel",
    "57": "Airtel",
    "98": "ZedMobile (Beeline)",
}

# Three-digit geographic area codes, all assigned to Zamtel.
GEOGRAPHIC_AREA_CODES: dict[str, str] = {
    "211": "Lusaka Province",
    "212": "Copperbelt",
    "213": "Livingstone",
    "214": "Kasama",
    "215": "Kabwe",
    "216": "Chipata",
    "217": "Solwezi",
    "218": "Mongu",
}

# VoIP NDC assigned to Liquid Telecom Zambia. These are ordinary subscriber
# numbers billed at normal rates, so they are callable.
VOIP_NDCS: dict[str, str] = {"63": "Liquid Telecom Zambia"}

# Special-rate ranges. Everything starting 8 (free-phone, local-rate) and 90
# (premium rate) is refused: a voice agent has no business dialling them, and
# 90 costs money.
SPECIAL_RATE_PREFIXES: tuple[str, ...] = ("8", "90")

SHORT_CODE_MIN = 3
SHORT_CODE_MAX = 5


class Category(str, Enum):
    MOBILE = "mobile"
    GEOGRAPHIC = "geographic"
    VOIP = "voip"
    PREMIUM = "premium"  # premium-rate and other special-rate ranges
    SHORT_CODE = "short_code"
    INTERNATIONAL = "international"
    INVALID = "invalid"


CALLABLE = frozenset({Category.MOBILE, Category.GEOGRAPHIC, Category.VOIP})


@dataclass(frozen=True)
class Classification:
    category: Category
    e164: str | None  # set whenever a full E.164 number could be derived
    detail: str | None = None  # operator or area, for documentation only

    @property
    def callable(self) -> bool:
        return self.category in CALLABLE


# Separators people and spreadsheets use. Anything else (letters, unicode
# digits, '*', '#', ';') is not stripped, so it makes the number invalid or a
# service code.
_SEPARATORS = re.compile(r"[\s\-\.\(\)/ ‐-―]")
_ASCII_DIGITS = re.compile(r"[0-9]+")
_SERVICE_CODE = re.compile(r"[*#][0-9*#]*#?|[0-9]{1,5}[*#][0-9*#]*")


def _clean(raw: str) -> str:
    return _SEPARATORS.sub("", raw)


def classify_nsn(nsn: str) -> Classification:
    """Classify a 9-digit Zambian national significant number."""
    if len(nsn) != NSN_LENGTH or not _ASCII_DIGITS.fullmatch(nsn):
        return Classification(Category.INVALID, None)
    e164 = f"+{COUNTRY_CODE}{nsn}"
    if nsn[:3] in GEOGRAPHIC_AREA_CODES:
        return Classification(Category.GEOGRAPHIC, e164, GEOGRAPHIC_AREA_CODES[nsn[:3]])
    if nsn[:2] in MOBILE_NDCS:
        return Classification(Category.MOBILE, e164, MOBILE_NDCS[nsn[:2]])
    if nsn[:2] in VOIP_NDCS:
        return Classification(Category.VOIP, e164, VOIP_NDCS[nsn[:2]])
    if nsn.startswith(SPECIAL_RATE_PREFIXES):
        return Classification(Category.PREMIUM, e164)
    # Reserved or free range: not a number anyone can hold today.
    return Classification(Category.INVALID, None, "unallocated range")


def classify(raw: str | None) -> Classification:
    """Normalise a raw dialled number and classify it.

    Accepted Zambian forms (separators: spaces, dashes, dots, slashes, brackets):
      0971234567, 971234567, +260971234567, 260971234567, 00260971234567,
      +260 (0)97 123 4567, +2600971234567 (stray trunk 0 after the country code).

    Anything with an international prefix ('+' or '00') for another country is
    ``international``. 3-5 digit strings and USSD/service codes ('*', '#') are
    ``short_code``. Everything else that is not a 9-digit Zambian NSN is
    ``invalid``.

    Note: in a URL query string an unencoded '+' arrives as a space. The
    dialplan must use ${URIENCODE()}; if it does not, '+260…' still works
    because 260 followed by 9 digits is recognised without the '+', but a
    foreign number then classifies as ``invalid`` instead of
    ``international`` (still denied).
    """
    if raw is None:
        return Classification(Category.INVALID, None)
    if len(raw) > MAX_RAW_LENGTH:
        return Classification(Category.INVALID, None)

    s = _clean(raw.strip())
    if not s:
        return Classification(Category.INVALID, None)

    if _SERVICE_CODE.fullmatch(s):
        return Classification(Category.SHORT_CODE, None)

    international = False
    if s.startswith("+"):
        international = True
        s = s[1:]
    elif s.startswith("00"):
        international = True
        s = s[2:]

    if not _ASCII_DIGITS.fullmatch(s):
        return Classification(Category.INVALID, None)

    if international:
        if not s.startswith(COUNTRY_CODE):
            # E.164 allows at most 15 digits including the country code.
            if 7 <= len(s) <= 15 and s[0] != "0":
                return Classification(Category.INTERNATIONAL, f"+{s}")
            return Classification(Category.INVALID, None)
        rest = s[len(COUNTRY_CODE):]
        if len(rest) == NSN_LENGTH + 1 and rest.startswith("0"):
            rest = rest[1:]
        if SHORT_CODE_MIN <= len(rest) <= SHORT_CODE_MAX:
            return Classification(Category.SHORT_CODE, None)
        return classify_nsn(rest)

    # No international prefix.
    if SHORT_CODE_MIN <= len(s) <= SHORT_CODE_MAX:
        return Classification(Category.SHORT_CODE, None)
    if len(s) == NSN_LENGTH + 1 and s.startswith("0"):
        return classify_nsn(s[1:])
    if len(s) == NSN_LENGTH and not s.startswith("0"):
        return classify_nsn(s)
    if s.startswith(COUNTRY_CODE):
        rest = s[len(COUNTRY_CODE):]
        if len(rest) == NSN_LENGTH + 1 and rest.startswith("0"):
            rest = rest[1:]
        if len(rest) == NSN_LENGTH:
            return classify_nsn(rest)
    return Classification(Category.INVALID, None)


def normalise(raw: str | None) -> str | None:
    """Return the Zambian E.164 form of a callable number, else None."""
    c = classify(raw)
    return c.e164 if c.callable else None
