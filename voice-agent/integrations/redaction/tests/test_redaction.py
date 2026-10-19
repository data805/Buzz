from collections import Counter

import pytest

from buzz_redaction import PLACEHOLDERS, luhn_valid, redact, redact_text

NRC = PLACEHOLDERS["nrc"]
CARD = PLACEHOLDERS["card"]
PIN = PLACEHOLDERS["pin"]
ID = PLACEHOLDERS["id"]
PHONE = PLACEHOLDERS["phone"]


@pytest.mark.parametrize(
    "digits, ok",
    [
        ("4111111111111111", True),  # Visa test number
        ("5555555555554444", True),  # Mastercard test number
        ("378282246310005", True),  # Amex test number
        ("4111111111111112", False),
        ("", False),
        ("41111111111x1111", False),
    ],
)
def test_luhn(digits, ok):
    assert luhn_valid(digits) is ok


# (input, expected output, expected counts) with phones=False
REDACT_CASES = [
    # NRC
    ("My NRC is 123456/78/1.", f"My NRC is {NRC}.", {"nrc": 1}),
    ("nrc 123456 / 78 / 1 thanks", f"nrc {NRC} thanks", {"nrc": 1}),
    # Cards (Luhn-valid only)
    ("card 4111 1111 1111 1111 exp", f"card {CARD} exp", {"card": 1}),
    ("card 4111-1111-1111-1111", f"card {CARD}", {"card": 1}),
    ("amex 3782 822463 10005", f"amex {CARD}", {"card": 1}),
    ("ref 4111 1111 1111 1112", "ref 4111 1111 1111 1112", {}),  # fails Luhn
    # PINs
    ("my PIN is 4321", f"my PIN is {PIN}", {"pin": 1}),
    ("the pin number is: 1 2 3 4.", f"the pin number is: {PIN}.", {"pin": 1}),
    ("OTP 654321 please", f"OTP {PIN} please", {"pin": 1}),
    ("my password is one two three four", f"my password is {PIN}", {"pin": 1}),
    ("CVV 123", f"CVV {PIN}", {"pin": 1}),
    ("the verification code was 908172", f"the verification code was {PIN}", {"pin": 1}),
    # Passport-like IDs
    ("passport ZN123456", f"passport {ID}", {"id": 1}),
    ("passport zn1234567 ok", f"passport {ID} ok", {"id": 1}),
    ("ID A12345678", f"ID {ID}", {"id": 1}),
    # Combined
    (
        "NRC 111111/11/1, card 5555555555554444, PIN 0000",
        f"NRC {NRC}, card {CARD}, PIN {PIN}",
        {"nrc": 1, "card": 1, "pin": 1},
    ),
]

# Must come through unchanged (false-positive guard), phones on and off.
UNCHANGED = [
    "The bootcamp starts on 19/10/2026 at 10:30.",
    "Call me back on 2026-10-19 at 14:00",
    "It costs K1,500 per month",
    "Fees are ZMW 2 500 or K150000 for the year",
    "We had 1200 attendees in 2025",
    "Room 12, floor 3, seat 45",
    "Pinned on the board at 1530",  # 'Pinned' is not the PIN trigger
    "Code Club meets 3 times a week",
    "Version 1.49.0 shipped",
    "Order ref 20261019",  # 8 digits: not a phone, not a card
    "The pin is in the map",  # trigger without digits
    # Dograh transcript line format (api/utils/transcript.py)
    "[2026-10-19T10:00:00.123+00:00] user: hello",
    "[2026-10-19T10:00:00.123+00:00 -> 2026-10-19T10:00:02.456+00:00] assistant: Hi!",
]

PHONE_CASES = [
    ("call 0971234567 today", f"call {PHONE} today"),
    ("call +260 97 123 4567", f"call {PHONE}"),
    ("call (0211) 378-200 now", f"call {PHONE} now"),
    ("or 260971234567.", f"or {PHONE}."),
]


@pytest.mark.parametrize("text, expected, counts", REDACT_CASES)
def test_redact_cases(text, expected, counts):
    r = redact_text(text)
    assert r.text == expected
    assert dict(r.counts) == counts


@pytest.mark.parametrize("text", UNCHANGED)
@pytest.mark.parametrize("phones", [False, True])
def test_false_positives(text, phones):
    r = redact_text(text, phones=phones)
    assert r.text == text
    assert r.total == 0


@pytest.mark.parametrize("text, expected", PHONE_CASES)
def test_phones_only_when_enabled(text, expected):
    assert redact_text(text).text == text
    assert redact_text(text, phones=True).text == expected


def test_nrc_not_card_or_phone():
    r = redact_text("123456/78/1", phones=True)
    assert r.text == NRC
    assert dict(r.counts) == {"nrc": 1}


def test_empty_and_none_like():
    assert redact_text("").text == ""
    assert redact_text("").total == 0


def test_redact_structure_recurses_and_counts():
    counts = Counter()
    value = {
        "summary": "Caller gave PIN 1234",
        "transcript": [
            {"role": "user", "content": "my NRC is 123456/78/1"},
            {"role": "assistant", "content": "Thanks."},
        ],
        "duration": 42,
        "flag": None,
    }
    out = redact(value, counts=counts)
    assert out["summary"] == f"Caller gave PIN {PIN}"
    assert out["transcript"][0]["content"] == f"my NRC is {NRC}"
    assert out["transcript"][1]["content"] == "Thanks."
    assert out["duration"] == 42 and out["flag"] is None
    assert counts == Counter({"pin": 1, "nrc": 1})
    # input not mutated
    assert value["summary"] == "Caller gave PIN 1234"
