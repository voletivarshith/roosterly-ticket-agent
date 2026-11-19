from __future__ import annotations

import re
from collections import Counter

# Each pattern replaces a sensitive value with a typed placeholder. The model only
# ever sees the placeholder, so the value cannot come back in a reply, note or log.

LINK_WITH_TOKEN = re.compile(
    r"https?://\S*(?:[?&](?:token|key|code|auth|sig|signature|reset|password)=|/reset/)\S+", re.I
)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
IBAN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}(?:[ ]?[A-Za-z0-9]){11,30}")
VAT_NUMBER = re.compile(r"[A-Za-z]{2}\d{9}[Bb]\d{2}")
CARD_CANDIDATE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
CVC = re.compile(r"\b(cvc2?|cvv2?|beveiligingscode|security code|sicherheitscode|prüfziffer)(\W{0,3})\d{3,4}\b", re.I)
EXPIRY = re.compile(r"\b(exp(?:iry|ires|\.)?|vervaldatum|geldig t/?m|gültig bis|valid thru)(\W{0,3})\d{2}\s?/\s?\d{2,4}\b", re.I)
BIC = re.compile(r"\b(bic|swift)(\W{0,3})[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", re.I)
ACCOUNT = re.compile(r"\b(rekeningnummer|rekeningnr|bankrekening|gironummer|account number|kontonummer)(\W{0,5})\d[\d .]{5,12}\d", re.I)
PASSWORD = re.compile(r"\b(wachtwoord|ww|password|passwort|kennwort|pincode|pin)(\s*[:=]\s*)(?=\S*[\d!@#$%^&*+])(\S{4,})", re.I)
SECRET = re.compile(
    r"\bBearer\s+[A-Za-z0-9._\-]{16,}"
    r"|\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"
    r"|\b(?:sk|pk|rk|api|key|token)[-_][A-Za-z0-9_\-]{16,}"
    r"|\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b"
)
ONE_TIME_CODE = re.compile(
    r"\b(verificatiecode|bevestigingscode|inlogcode|verification code|login code|bestätigungscode|otp|2fa[- ]code|sms[- ]code)"
    r"(\W{0,3})\d{4,8}\b",
    re.I,
)
BSN_CANDIDATE = re.compile(r"(?<!\d)\d{9}(?!\d)|(?<!\d)\d{4}\.\d{2}\.\d{3}(?!\d)")
PHONE = re.compile(r"(?<![\w+])(?:\+|00)\d{1,3}[\d ()\-]{7,14}\d|(?<![\d\w])0\d{1,3}[ -]?\d{6,8}(?!\d)")


def redact(text: str) -> tuple[str, Counter[str]]:
    """Return the text with sensitive values replaced, and how many of each kind."""
    counts: Counter[str] = Counter()
    text = _swap(text, counts, LINK_WITH_TOKEN, "link_with_token")
    text = _swap(text, counts, EMAIL, "email")
    text = _redact_ibans(text, counts)
    text = _redact_cards(text, counts)
    for pattern, kind in ((CVC, "cvc"), (EXPIRY, "expiry"), (BIC, "bic"), (ACCOUNT, "account"), (PASSWORD, "password")):
        text = _swap(text, counts, pattern, kind, keep_groups=2)  # keep the keyword, drop the value
    text = _swap(text, counts, SECRET, "secret")
    text = _swap(text, counts, ONE_TIME_CODE, "code", keep_groups=2)
    text = _redact_bsns(text, counts)
    text = _swap(text, counts, PHONE, "phone")
    return text, counts


def _swap(text: str, counts: Counter[str], pattern: re.Pattern, kind: str, keep_groups: int = 0) -> str:
    def replace(match: re.Match) -> str:
        counts[kind] += 1
        kept = "".join(match.group(i) for i in range(1, keep_groups + 1))
        return f"{kept}[{kind.upper()}]"

    return pattern.sub(replace, text)


def contains_sensitive(text: str) -> bool:
    return bool(redact(text)[1])


def _redact_ibans(text: str, counts: Counter[str]) -> str:
    def replace(match: re.Match) -> str:
        candidate = match.group(0)
        end = _iban_end(candidate)
        if end is None:
            return candidate
        counts["iban"] += 1
        return "[IBAN]" + candidate[end:]

    return IBAN_CANDIDATE.sub(replace, text)


def _iban_end(candidate: str) -> int | None:
    """Where the IBAN stops inside a candidate that may have swallowed the next words."""
    if VAT_NUMBER.match(candidate.replace(" ", "")):
        return None
    stops = [i + 1 for i, ch in enumerate(candidate) if i + 1 == len(candidate) or candidate[i + 1] == " "]
    for stop in reversed(stops):  # longest first: prefer a span with a valid checksum
        if _iban_checksum_ok(candidate[:stop].replace(" ", "")):
            return stop
    # No valid checksum: a mistyped IBAN is still a payment detail. Cut after the last digit.
    last_digit = max(i for i, ch in enumerate(candidate) if ch.isdigit()) + 1
    span = candidate[:last_digit].replace(" ", "")
    if 15 <= len(span) <= 34 and sum(ch.isdigit() for ch in span) >= 10:
        return last_digit
    return None


def _iban_checksum_ok(compact: str) -> bool:
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = (compact[4:] + compact[:4]).upper()
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


def _redact_cards(text: str, counts: Counter[str]) -> str:
    def replace(match: re.Match) -> str:
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        grouped = re.fullmatch(r"\d{4}(?:[ -]\d{4}){3}", candidate) is not None
        if not (_luhn_ok(digits) or grouped):
            return candidate
        counts["card"] += 1
        return "[CARD]"

    return CARD_CANDIDATE.sub(replace, text)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for position, ch in enumerate(reversed(digits)):
        value = int(ch)
        if position % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _redact_bsns(text: str, counts: Counter[str]) -> str:
    def replace(match: re.Match) -> str:
        digits = match.group(0).replace(".", "")
        if not _bsn_ok(digits):
            return match.group(0)
        counts["bsn"] += 1
        return "[BSN]"

    return BSN_CANDIDATE.sub(replace, text)


def _bsn_ok(digits: str) -> bool:
    """The Dutch "11-proof": weights 9..2 and -1, sum divisible by 11."""
    if digits == "000000000":
        return False
    weights = (9, 8, 7, 6, 5, 4, 3, 2, -1)
    return sum(int(d) * w for d, w in zip(digits, weights)) % 11 == 0
