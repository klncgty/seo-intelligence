"""Topic extraction and first-party-demand-backed content gap analysis."""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import unquote, urlsplit

from app.services.competitors.page_types import classify_page
from app.services.competitors.types import OwnQuery, PageSnapshot, SiteSnapshot

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_STOPWORDS = frozenset("""
a an the and or but if then than so as at by for from in into of on onto to up via with
is are was were be been being have has had do does did can could may might must shall
should will would this that these those it its we our you your they their he she them
about after all also any because before between both each few more most other some such
no nor not only own same too very what when where which who why how
i me my mine us ours yours theirs himself herself itself themselves
ve veya ile için gibi bir bu şu o da de ki mi mu mı mü ama ancak daha en çok her
hiç hangi nasıl neden ne nerede niçin niye olan olarak tarafından kadar sonra önce
ise veya ya hem çünkü fakat fakatki tüm bütün bazı başka diğer kendi bizim sizin
onlar biz siz ben bana beni benim sana seni senin ona onu onun bunlar şunlar
""".split())
_EXCLUDED_TYPES = frozenset({"login_account", "legal", "contact", "careers"})
_TOPIC_RE = re.compile(r"^[^\W\d_]+(?: [^\W\d_]+)?$", re.UNICODE)


def _stem_english(token: str) -> str:
    if len(token) < 4:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "xes", "zes", "oes", "sses")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase and split Unicode words, removing stopwords and light EN plurals."""
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        token = _stem_english(raw)
        if len(token) < 3 or token.isnumeric() or token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def _topics_for_page(page: PageSnapshot) -> tuple[set[str], set[str]]:
    source = " ".join(filter(None, [page.title, *page.h1]))
    title_h1 = tokenize(source)
    slug_tokens = tokenize(unquote(urlsplit(page.url).path.replace("/", " ")))
    topics = set(title_h1) | set(slug_tokens)
    topics.update(f"{left} {right}" for left, right in zip(title_h1, title_h1[1:]))
    topics.update(f"{left} {right}" for left, right in zip(slug_tokens, slug_tokens[1:]))
    return topics, {
        topic for topic in topics
        if topic in set(slug_tokens)
        or any(topic == f"{left} {right}" for left, right in zip(slug_tokens, slug_tokens[1:]))
    }


def extract_topics(pages: list[PageSnapshot]) -> dict[str, list[str]]:
    """Return qualifying normalized topics mapped to sorted unique page URLs."""
    urls_by_topic: dict[str, set[str]] = defaultdict(set)
    slug_topics: set[str] = set()
    for page in pages:
        if page.status_code != 200 or not page.is_html:
            continue
        if classify_page(page) in _EXCLUDED_TYPES:
            continue
        topics, from_slug = _topics_for_page(page)
        slug_topics.update(from_slug)
        for topic in topics:
            if _TOPIC_RE.fullmatch(topic):
                urls_by_topic[topic].add(page.url)
    return {
        topic: sorted(urls)
        for topic, urls in sorted(urls_by_topic.items())
        if len(urls) >= 2 or topic in slug_topics
    }


def content_gaps(
    own: SiteSnapshot,
    competitors: list[SiteSnapshot],
    own_queries: list[OwnQuery],
) -> list[dict]:
    """Find competitor topics absent from the site's own crawled page topics."""
    own_topics = extract_topics(own.pages)
    topic_competitors: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for site in competitors:
        for topic, urls in extract_topics(site.pages).items():
            topic_competitors[topic][site.label].update(urls)

    gaps: list[dict] = []
    for topic, competitor_map in topic_competitors.items():
        if topic in own_topics:
            continue
        topic_tokens = set(topic.split())
        matches = [
            row for row in own_queries
            if topic_tokens.issubset(set(tokenize(row.query)))
        ]
        matches.sort(key=lambda row: (-row.impressions, -row.clicks, row.query))
        impressions = sum(row.impressions for row in matches)
        clicks = sum(row.clicks for row in matches)
        competitors_for_topic = [
            {
                "label": label,
                "urls": sorted(urls)[:5],
                "page_count": len(urls),
            }
            for label, urls in sorted(competitor_map.items())
        ]
        evidence = None
        if matches:
            evidence = {
                "matched_queries": [
                    {"query": row.query, "impressions": row.impressions, "clicks": row.clicks}
                    for row in matches[:10]
                ],
                "impressions": impressions,
                "clicks": clicks,
            }
        gaps.append({
            "topic": topic,
            "competitors": competitors_for_topic,
            "own_gsc_evidence": evidence,
            "evidence_level": "own_demand" if impressions > 0 else "competitor_only",
        })

    gaps.sort(key=lambda gap: (
        0 if gap["evidence_level"] == "own_demand" else 1,
        -(gap["own_gsc_evidence"]["impressions"] if gap["own_gsc_evidence"] else 0),
        -len(gap["competitors"]),
        gap["topic"],
    ))
    return gaps
