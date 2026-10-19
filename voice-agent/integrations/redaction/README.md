# Transcript redaction

`buzz_redaction` is a pure-Python module with no dependencies. It runs regex redaction over transcripts and summaries before anything goes to HubSpot or Google Sheets (PLAN section 7). n8n calls it through `POST /v1/redact` on the suppression service, which bundles this package.

```python
from buzz_redaction import redact_text, redact

redact_text("my PIN is 4321").text        # 'my PIN is [REDACTED:PIN]'
redact({"summary": "...", "turns": [...]}, phones=True)   # recursive over JSON values
```

| Kind | Rule | Placeholder |
|---|---|---|
| NRC | `######/##/#`, with spaces allowed around the slashes | `[REDACTED:NRC]` |
| Card | 13–19 digits, optionally grouped with spaces or dashes, and the Luhn check must pass | `[REDACTED:CARD]` |
| PIN or code | 3–8 digits, or spoken digit words, within 30 characters after a trigger word: PIN, passcode, password, OTP, CVV/CVC, security, verification, access or secret code | `[REDACTED:PIN]` |
| Passport-like ID | 1–2 letters followed by 6–8 digits (Zambian passports are `ZN` plus 6 digits). A lone `K` prefix is excluded so that kwacha amounts survive. | `[REDACTED:ID]` |
| Phone (only with `phones=True`) | 9–15 digits, with optional `+` or brackets and separators | `[REDACTED:PHONE]` |

**Left alone on purpose** (each has a false-positive test): dates, times, `K1,500`, `ZMW 2 500`, `K150000`, years, version numbers, 8-digit references, and the timestamps in Dograh transcript lines.

**Known limits:**
- A card number that fails the Luhn check is not redacted. This includes card numbers the speech-to-text got wrong.
- A PIN spoken without a trigger word is not caught.
- Free-text addresses and names are not redacted.

This is a safety net. The primary control is that agents never ask for these data.

Tests: run `pytest` in this folder.
