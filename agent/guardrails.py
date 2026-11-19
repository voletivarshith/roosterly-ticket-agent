from __future__ import annotations

import re

from agent.knowledge import HelpCentre
from agent.redact import contains_sensitive
from agent.schema import Evidence

# Checks a drafted reply must pass before it can be sent. None of them needs
# anyone to read Dutch: they compare the draft with the help-centre text.

MIN_CHARS, MAX_CHARS = 40, 1200
MIN_QUOTE_CHARS = 15

ARROW = "→"
ARROW_VARIANTS = re.compile(r"\s*(?:->|–>|—>|›|»|\s>\s)\s*")
SEGMENT = r"[A-ZÀ-Ý][\w\-]*(?: [a-zà-ÿ][\w\-]*)*"
MENU_PATH = re.compile(rf"{SEGMENT}(?:\s*{ARROW}\s*{SEGMENT})+")
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.M)
DOMAIN = re.compile(
    r"(?:https?://|www\.)?(?:[\w-]+\.)+(?:nl|com|org|net|io|eu|de|be|app|co|info|ai|dev|uk|fr)\b(?:/[\w\-./]*)?", re.I
)

# Words that are common in one of the three languages and absent from the other two.
STOP_WORDS = {
    "nl": set("de het een en van niet voor naar je jij jouw uw u ik mijn kunt kun kan wij onze hebt heeft geen bij "
              "ook maar wordt worden deze dit zijn aan om te op met dat als nog dan wel hoi beste groet groeten "
              "bedankt".split()),
    "en": set("the and you your not that this for with are can our have has will but there then from please "
              "to if at it be by on hello hi dear regards thanks thank".split()),
    "de": set("der das und sie ihr ihre ihren nicht dass für mit auf zu ist sind können kann auch wenn wir unser "
              "unsere haben hat wird werden oder aber kein keine bitte unter über ein eine einen den dem des bei "
              "von ich es hallo grüße danke".split()),
}


def check_reply(draft: str, evidence: list[Evidence], language: str, help_centre: HelpCentre,
                ticket_text: str = "") -> list[str]:
    """Return every problem with a drafted reply; an empty list means it may go on."""
    problems: list[str] = []
    draft = ARROW_VARIANTS.sub(f" {ARROW} ", draft)

    if not MIN_CHARS <= len(draft.strip()) <= MAX_CHARS:
        problems.append(f"the reply must be {MIN_CHARS} to {MAX_CHARS} characters long")
    if contains_sensitive(draft):
        problems.append("the reply contains a payment detail, credential or contact detail")

    problems += _evidence_problems(evidence, help_centre)
    cited = help_centre.text_of(e.faq_id for e in evidence)

    allowed_numbers = _allowed_numbers(cited, ticket_text)
    for number in NUMBER.findall(LIST_MARKER.sub("", draft)):
        if not _readings(number) & allowed_numbers:
            problems.append(f"the number {number} is not in the cited help-centre articles")

    for domain in DOMAIN.findall(draft):
        cleaned = re.sub(r"^https?://", "", domain.lower()).rstrip("./")
        if cleaned not in cited.lower():
            problems.append(f"the address {domain} is not in the cited help-centre articles")

    problems += _menu_path_problems(draft, cited)

    written_in = detect_language(draft)
    if written_in and written_in != language:
        problems.append(f"the reply is written in {written_in} but the customer wrote in {language}")
    return problems


def detect_language(text: str) -> str | None:
    """nl, en or de by counting common words; None when the text is too short to tell."""
    words = re.findall(r"[a-zß-ÿ]+", text.lower())
    scores = {lang: sum(word in stop_words for word in words) for lang, stop_words in STOP_WORDS.items()}
    best, runner_up = sorted(scores.values(), reverse=True)[:2]
    if best < 3 or best < 1.5 * runner_up:
        return None
    return max(scores, key=lambda language: scores[language])


def _evidence_problems(evidence: list[Evidence], help_centre: HelpCentre) -> list[str]:
    if not evidence:
        return ["a reply needs at least one evidence quote from the help centre"]
    problems = []
    for item in evidence:
        article = help_centre.articles.get(item.faq_id)
        quote = _normalise_quote(item.quote)
        if article is None:
            problems.append(f"{item.faq_id} is not a help-centre article")
        elif len(quote) < MIN_QUOTE_CHARS or quote not in _normalise_quote(article.text):
            problems.append(f"the quote for {item.faq_id} is not copied exactly from that article")
    return problems


def _menu_path_problems(draft: str, cited: str) -> list[str]:
    known_steps = set()
    for path in MENU_PATH.findall(cited):
        segments = [segment.strip().lower() for segment in path.split(ARROW)]
        known_steps.update(zip(segments, segments[1:]))

    problems = []
    for match in re.finditer(ARROW, draft):
        before = draft[: match.start()].rstrip().lower()
        after = draft[match.end():].lstrip().lower()
        if not any(before.endswith(a) and after.startswith(b) for a, b in known_steps):
            shown = f"{before[-25:]} {ARROW} {after[:25]}"
            problems.append(f"the menu path '{shown}' is not in the cited help-centre articles")
    return problems


def _allowed_numbers(cited: str, ticket_text: str) -> set[str]:
    """Numbers a reply may contain: those in the cited articles, those the customer wrote,
    and one of each multiplied, which is a unit price times the customer's quantity."""
    from_articles = {_normalise_number(n) for n in NUMBER.findall(cited)}
    from_ticket = {_normalise_number(n) for n in NUMBER.findall(ticket_text)}
    totals = {_normalise_number(f"{float(a) * float(b):.2f}") for a in from_articles for b in from_ticket}
    return from_articles | from_ticket | totals


def _readings(number: str) -> set[str]:
    """How a written number can be read: 1.200 is 1200 in Dutch and 1.2 in English."""
    readings = {_normalise_number(number)}
    whole, _, rest = number.replace(",", ".").partition(".")
    if len(rest) == 3:
        readings.add(whole + rest)
    return readings


def _normalise_number(number: str) -> str:
    number = number.replace(",", ".")
    return number.rstrip("0").rstrip(".") if "." in number else number


def _normalise_quote(text: str) -> str:
    """Spacing and the style of quotation marks do not count: a model often writes "…" as '…'."""
    for mark in "“”„‘’'":
        text = text.replace(mark, '"')
    return " ".join(text.split()).strip(' ".')
