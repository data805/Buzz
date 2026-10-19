import pytest

from suppression.numbers import (
    GEOGRAPHIC_AREA_CODES,
    MOBILE_NDCS,
    Category,
    classify,
    classify_nsn,
    normalise,
)

M = Category.MOBILE
G = Category.GEOGRAPHIC
V = Category.VOIP
P = Category.PREMIUM
S = Category.SHORT_CODE
I = Category.INTERNATIONAL
X = Category.INVALID

# (raw input, expected category, expected e164)
TABLE = [
    # --- accepted Zambian mobile formats --------------------------------
    ("0971234567", M, "+260971234567"),
    ("971234567", M, "+260971234567"),
    ("+260971234567", M, "+260971234567"),
    ("260971234567", M, "+260971234567"),
    ("00260971234567", M, "+260971234567"),
    ("+260 97 123 4567", M, "+260971234567"),
    ("097-123-4567", M, "+260971234567"),
    ("(097) 123 4567", M, "+260971234567"),
    ("+260 (0)97 123 4567", M, "+260971234567"),
    ("+2600971234567", M, "+260971234567"),
    ("2600971234567", M, "+260971234567"),
    ("  0971234567  ", M, "+260971234567"),
    ("097.123.4567", M, "+260971234567"),
    ("0971 234 567", M, "+260971234567"),  # non-breaking spaces
    (" 260971234567", M, "+260971234567"),  # unencoded '+' arrives as a space
    # --- every assigned mobile NDC ----------------------------------------
    ("0951234567", M, "+260951234567"),  # Zamtel
    ("0751234567", M, "+260751234567"),  # Zamtel
    ("0961234567", M, "+260961234567"),  # MTN
    ("0761234567", M, "+260761234567"),  # MTN
    ("0771234567", M, "+260771234567"),  # Airtel
    ("0571234567", M, "+260571234567"),  # Airtel (from 23/12/2024)
    ("0981234567", M, "+260981234567"),  # ZedMobile (Beeline)
    # --- geographic --------------------------------------------------------
    ("0211378200", G, "+260211378200"),  # ZICTA's own number, Lusaka
    ("+260 211 246701", G, "+260211246701"),
    ("0212123456", G, "+260212123456"),
    ("0218123456", G, "+260218123456"),
    # --- VoIP (Liquid) -------------------------------------------------------
    ("0631234567", V, "+260631234567"),
    # --- premium / special rate ---------------------------------------------
    ("0901234567", P, "+260901234567"),  # 90 premium rate
    ("0800123456", P, "+260800123456"),  # 800 free-phone
    ("0851234567", P, "+260851234567"),  # 8x special services
    ("+260800123456", P, "+260800123456"),
    # --- short codes and service codes -------------------------------------
    ("112", S, None),
    ("991", S, None),
    ("999", S, None),
    ("1234", S, None),
    ("12345", S, None),
    ("+260999", S, None),
    ("*115#", S, None),
    ("*303*1#", S, None),
    # --- international ------------------------------------------------------
    ("+27821234567", I, "+27821234567"),  # South Africa
    ("0027821234567", I, "+27821234567"),
    ("+44 20 7946 0000", I, "+442079460000"),
    ("+1 (202) 555-0100", I, "+12025550100"),
    ("+263771234567", I, "+263771234567"),  # Zimbabwe, starts with 26 but not 260
    # --- invalid --------------------------------------------------------------
    ("", X, None),
    ("   ", X, None),
    ("abc", X, None),
    ("097123456", X, None),  # too short after trunk 0
    ("09712345678", X, None),  # too long
    ("211378", X, None),  # 6-digit local fixed number: area code required
    ("0551234567", X, None),  # 55 reserved for Zamtel, not assigned
    ("0561234567", X, None),  # 56 reserved for MTN
    ("0581234567", X, None),  # 58 reserved for Beeline
    ("0781234567", X, None),  # 78 reserved for Beeline
    ("0791234567", X, None),  # 79 free
    ("0991234567", X, None),  # 99 free
    ("0501234567", X, None),  # 50 free
    ("0219123456", X, None),  # 219 free
    ("0210123456", X, None),  # 210 reserved
    ("0201234567", X, None),  # 20 reserved
    ("0111234567", X, None),  # 1 reserved
    ("0601234567", X, None),  # 60 VoIP free
    ("+260", X, None),
    ("+", X, None),
    ("00", X, None),
    ("+0123456789", X, None),
    ("0971234567x12", X, None),
    ("0971234567;", X, None),
    ("٠٩٧١٢٣٤٥٦٧", X, None),  # Arabic-Indic digits
    ("27821234567", X, None),  # foreign number without '+' or '00'
    ("+2601234567890123", X, None),
    ("0" * 65, X, None),  # longer than MAX_RAW_LENGTH
]


@pytest.mark.parametrize("raw, category, e164", TABLE)
def test_classification_table(raw, category, e164):
    c = classify(raw)
    assert c.category is category, raw
    assert c.e164 == e164, raw


def test_none_is_invalid():
    assert classify(None).category is Category.INVALID


@pytest.mark.parametrize("ndc", sorted(MOBILE_NDCS))
def test_every_mobile_ndc_is_callable(ndc):
    c = classify_nsn(ndc + "1234567")
    assert c.category is Category.MOBILE and c.callable


@pytest.mark.parametrize("code", sorted(GEOGRAPHIC_AREA_CODES))
def test_every_area_code_is_callable(code):
    c = classify_nsn(code + "123456")
    assert c.category is Category.GEOGRAPHIC and c.callable


def test_every_two_digit_prefix_is_classified_exactly_once():
    """Exhaustive sweep of 9-digit NSNs by first two digits against the ITU table."""
    callable_2 = set(MOBILE_NDCS) | {"63"}
    for a in range(10):
        for b in range(10):
            prefix = f"{a}{b}"
            c = classify_nsn(prefix + "1234567")
            if prefix == "21":
                continue  # depends on the third digit, covered above
            if prefix in callable_2:
                assert c.callable, prefix
            elif prefix.startswith("8") or prefix == "90":
                assert c.category is Category.PREMIUM, prefix
            else:
                assert c.category is Category.INVALID, prefix


@pytest.mark.parametrize(
    "raw, expected",
    [("0971234567", "+260971234567"), ("0901234567", None), ("112", None), ("+27821234567", None)],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_classify_nsn_rejects_bad_input():
    assert classify_nsn("97123456").category is Category.INVALID
    assert classify_nsn("97123456a").category is Category.INVALID
