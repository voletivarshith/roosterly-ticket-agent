from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from langchain_text_splitters import MarkdownHeaderTextSplitter

# help_centre.md holds the articles of the client's faq.md, copied unchanged.
# The client's rules from that file are the table in agent/policy.py.
HELP_CENTRE_PATH = Path(__file__).resolve().parent.parent / "help_centre.md"


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    text: str


@dataclass(frozen=True)
class HelpCentre:
    """The only knowledge the agent may answer from."""

    articles: dict[str, Article]

    def text_of(self, faq_ids: Iterable[str]) -> str:
        return "\n".join(self.articles[i].text for i in faq_ids if i in self.articles)

    def as_prompt(self) -> str:
        return "\n\n".join(f"{a.id} · {a.title}\n{a.text}" for a in self.articles.values())


def load_help_centre(path: Path | str = HELP_CENTRE_PATH) -> HelpCentre:
    """One article per '## F01 · Title' heading."""
    sections = MarkdownHeaderTextSplitter([("##", "heading")]).split_text(Path(path).read_text(encoding="utf-8"))
    articles: dict[str, Article] = {}
    for section in sections:
        article_id, _, title = section.metadata.get("heading", "").partition(" · ")
        if not title:
            raise SystemExit(f"{path}: every article needs a '## F01 · Title' heading")
        articles[article_id] = Article(article_id, title.strip(), " ".join(section.page_content.split()))
    if not articles:
        raise SystemExit(f"{path}: no articles found")
    return HelpCentre(articles)
