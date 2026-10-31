"""Deterministic page-type classification from URL, title, and H1 text."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.services.competitors.types import PageSnapshot

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _text(page: PageSnapshot) -> str:
    path = urlsplit(page.url).path.lower()
    text = " ".join((path, page.title or "", *page.h1)).lower()
    return " ".join(_WORD_RE.findall(text))


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(f" {phrase} " in padded for phrase in phrases)


def classify_page(page: PageSnapshot) -> str:
    """Classify a page using URL structure and English/Turkish page signals."""
    path = urlsplit(page.url).path.rstrip("/").lower()
    segments = [part for part in path.split("/") if part]
    text = _text(page)

    if not segments:
        return "home"
    if _has(text, ("login", "log in", "signin", "sign in", "account", "cart",
                   "checkout", "giris", "giriş", "hesap", "sepet", "odeme", "ödeme")):
        return "login_account"
    if _has(text, ("privacy", "privacy policy", "terms", "terms of service", "legal",
                   "gizlilik", "kullanim sartlari", "kullanım şartları", "kvkk")):
        return "legal"
    if _has(text, ("contact", "contact us", "iletisim", "iletişim", "bize ulasin",
                   "bize ulaşın")):
        return "contact"
    if _has(text, ("careers", "career", "jobs", "job", "kariyer", "is ilanlari",
                   "iş ilanları")):
        return "careers"
    if _has(text, ("about", "about us", "hakkimizda", "hakkımızda", "kurumsal")):
        return "about"
    if _has(text, ("vs", "versus", "comparison", "compare", "karşılaştırma",
                   "karsilastirma", "kiyaslama", "kıyaslama")):
        return "comparison"
    if _has(text, ("alternatives", "alternative", "alternatifleri", "alternatif")):
        return "alternatives"
    if _has(text, ("pricing", "price", "plans", "planlar", "fiyat", "fiyatlandirma",
                   "fiyatlandırma", "ücret", "ucret", "ücretler", "ucretler")):
        return "pricing"
    if _has(text, ("integration", "integrations", "integrates", "entegrasyon",
                   "entegrasyonlar")):
        return "integration"
    if _has(text, ("case study", "case studies", "customer story", "customer stories",
                   "success story", "success stories", "müşteri hikayesi",
                   "musteri hikayesi", "basari hikayesi", "başarı hikayesi",
                   "vaka analizi")):
        return "case_study"
    if _has(text, ("faq", "frequently asked questions", "sik sorulan sorular",
                   "sık sorulan sorular")):
        return "faq"
    if _has(text, ("glossary", "dictionary", "sözlük", "sozluk", "terimler")):
        return "glossary"
    if _has(text, ("documentation", "docs", "help center", "knowledge base",
                   "dokumantasyon", "dokümantasyon", "yardim merkezi", "yardım merkezi")):
        return "docs"
    if _has(text, ("blog", "articles", "article", "news", "haberler", "makaleler")):
        blog_segment = any(
            segment in {"blog", "blogs", "articles", "news"}
            or segment.startswith(("blog-", "article-", "news-"))
            for segment in segments
        )
        if blog_segment and len(segments) > 1:
            return "blog_article"
        if _has(text, ("article", "articles", "news", "haberler", "makaleler")):
            return "blog_article"
        return "blog_index"
    if _has(text, ("product", "products", "urun", "ürün", "ürünler", "urunler")):
        return "product"
    if _has(text, ("category", "categories", "collection", "collections", "kategori",
                   "kategoriler", "koleksiyon")):
        return "category"
    if len(segments) == 1 and segments[0] in {"blog", "blogs", "articles", "news"}:
        return "blog_index"
    if _has(text, ("feature", "features", "özellik", "ozellik", "solution", "solutions",
                   "cozum", "çözüm", "landing")):
        return "landing/feature"
    return "other"
