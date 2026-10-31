"""Locale-aware text normalization (Turkish first-class, English equal quality).

- tr_lower: Turkish casing (İ→i, I→ı) — Python's str.lower() maps "I"→"i" and "İ"→"i̇" (wrong for TR).
- match_key: diacritic-insensitive key for matching/dedupe (ç→c, ğ→g, ı→i, ö→o, ş→s, ü→u, accents stripped);
  original text is always kept separately.
- normalize_keyword: trim/collapse whitespace, locale lowercase, strip surrounding punctuation.
- stem: Snowball (turkish / english).
- detect_language: lightweight heuristic (TR letters + stopword hits); returns "tr", "en" or "unknown".
"""

import re
import unicodedata
from functools import lru_cache

import snowballstemmer

_WS = re.compile(r"\s+")
_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
_TR_MAP = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})
TR_STOP = {"ve", "ile", "bir", "bu", "için", "nasıl", "nedir", "ne", "en", "mi", "mı", "mu", "mü", "da", "de", "gibi",
           "olan", "çok", "daha", "hangi", "neden", "kaç", "fiyat", "fiyatları", "en iyi"}
EN_STOP = {"the", "and", "for", "with", "how", "what", "is", "to", "of", "in", "best", "a", "an", "vs", "why", "which"}
QUESTION_WORDS = {"tr": ("nasıl", "nedir", "ne", "neden", "niçin", "hangi", "hangisi", "kaç", "kim", "nerede", "mi", "mı", "mu", "mü"),
                  "en": ("how", "what", "why", "which", "who", "where", "when", "can", "does", "is", "are", "should")}


def tr_lower(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def tr_upper(s: str) -> str:
    return s.replace("i", "İ").replace("ı", "I").upper()


def locale_lower(s: str, lang: str = "tr") -> str:
    return tr_lower(s) if lang in ("tr", "az") else s.lower()


def match_key(s: str, lang: str = "tr") -> str:
    # NFKD first (compatibility forms like "𝓐" → "A"), then locale lowercase, then fold diacritics
    decomposed = unicodedata.normalize("NFKD", locale_lower(unicodedata.normalize("NFKC", s), lang))
    folded = locale_lower("".join(c for c in decomposed if not unicodedata.combining(c)), lang).translate(_TR_MAP)
    return _WS.sub(" ", folded).strip()


def normalize_keyword(s: str, lang: str = "tr") -> str:
    s = unicodedata.normalize("NFC", s)
    s = _WS.sub(" ", s).strip()
    s = _EDGE_PUNCT.sub("", s)
    return locale_lower(s, lang)


@lru_cache(maxsize=4)
def _stemmer(lang: str):
    return snowballstemmer.stemmer({"tr": "turkish", "en": "english"}.get(lang, "english"))


_TR_VOWELS = set("aeıioöuü")


def _tr_pre(token: str) -> str:
    """Snowball misses the buffer consonant of the 3rd-person possessive after a vowel
    (ayakkabı-sı, araba-sı): strip "-sı/-si/-su/-sü" when preceded by a vowel."""
    if len(token) > 5 and token[-2:] in ("sı", "si", "su", "sü") and token[-3] in _TR_VOWELS:
        return token[:-2]
    return token


def stem_tokens(s: str, lang: str = "tr") -> list[str]:
    tokens = [t for t in re.split(r"[^\w]+", locale_lower(s, lang)) if t]
    if lang == "tr":
        tokens = [_tr_pre(t) for t in tokens]
    return _stemmer(lang).stemWords(tokens)


def detect_language(s: str) -> str:
    low = tr_lower(s)
    tokens = set(re.split(r"[^\w]+", low))
    tr_score = sum(c in "çğıöşü" for c in low) * 2 + len(tokens & TR_STOP)
    en_score = len(tokens & EN_STOP)
    if tr_score == en_score:
        return "unknown"
    return "tr" if tr_score > en_score else "en"


def is_question(s: str, lang: str = "tr") -> bool:
    low = locale_lower(s, lang).strip()
    if low.endswith("?"):
        return True
    words = re.split(r"[^\w]+", low)
    qs = QUESTION_WORDS.get(lang, QUESTION_WORDS["en"])
    if lang == "tr":  # Turkish question words / particles can appear anywhere ("… nasıl seçilir", "… iyi mi")
        return any(w in qs for w in words)
    return bool(words) and words[0] in qs


def dedupe_keywords(keywords: list[str], lang: str = "tr") -> list[dict]:
    """Group exact duplicates (after normalization) and close variants (same match key / stem set)."""
    groups: dict[str, dict] = {}
    for kw in keywords:
        norm = normalize_keyword(kw, lang)
        if not norm:
            continue
        # fold diacritics first so ASCII-typed queries ("kosu ayakkabisi") group with the original
        variant_key = " ".join(sorted(stem_tokens(match_key(norm, lang), lang)))
        g = groups.setdefault(variant_key, {"canonical": norm, "variants": []})
        if norm not in g["variants"]:
            g["variants"].append(norm)
    return list(groups.values())
