# PROJECT AUDIT — SEO Growth Intelligence Platform

**Son Güncelleme:** 2026-10-30  
**Proje Dizin Yolu:** `/Users/cagataykilinc/projeler/seo-intelligence`  
**Mimari Durumu:** Kurumsal Düzey (Enterprise-Grade) Semrush/Ahrefs Eşdeğeri SEO Growth & Competitor Intelligence Platformu + Otonom Deep Code Analysis & Claude Code Fix Agent.

---

## 1. Mimari ve Altyapı Özeti

Proje, çok kiracılı (multi-tenant), kaynak sınıfı doğrulamalı (`FIRST_PARTY`, `OBSERVED`, `PROVIDER`, `MODELED`, `AI`, `DEMO`) tam teşekküllü bir SEO platformudur:

- **AI Katmanı:** Doğrudan yerel **Claude Code CLI** (`claude -p` non-interactive print mode, JSON format). Harici Anthropic API anahtarı veya üçüncü taraf LLM SDK bağımlılığı yoktur (`mode: direct_cli_no_api`). Kota aşımı (`CLAUDE_CODE_RATE_LIMITED`) durumunda sistem çökmez; kota sıfırlama zamanını raporlar ve deterministik motorlarla çalışmaya kesintisiz devam eder.
- **Backend:** Python 3.13, FastAPI 0.162, SQLAlchemy 2.0, Alembic (`0001`–`0020` PostgreSQL migration'ları), HTTPX, lxml, SnowballStemmer, Hypothesis. **281 backend testi %100 PASS!**
- **Veri Katmanı:**
  - **PostgreSQL 17:** OLTP, çok kiracılı RLS (Row-Level Security), projeler, siteler, taramalar (crawl), denetim bulguları (audit findings), büyüme fırsatları, iş kuyruğu (`jobs` + DAG + `SKIP LOCKED`), kredi defteri, şifreli entegrasyon anahtarları, PPC anahtar kelimeleri (`ppc_keywords`), link building adayları (`link_prospects`), sosyal medya hesapları/postları (`social_accounts`, `social_posts`), AI SEO teşhis kayıtları (`seo_diagnoses`), kod depoları ve onay kapılı düzeltme önerileri (`code_repositories`, `code_fix_proposals`).
  - **ClickHouse:** Büyük hacimli analitik tablolar (`keyword_metrics`, `keyword_latest`, `serp_results`, `domain_keyword_rankings`, `domain_ranked_keywords`, `rank_tracking_results`, `backlinks`, `domain_strength`, `ai_visibility_results` — `0001`–`0004` migration'ları).
  - **Redis:** Host bazlı ortak nezaket (politeness) token bucket hız sınırlaması ve sağlayıcı önbelleği.
  - **MinIO:** Content-addressed WARC/1.1 arşivleri ve ham sağlayıcı yanıtları.
- **Frontend:** React 18, TypeScript 5.9, Vite 5, Tailwind CSS, TanStack Query v5, TanStack Table v8, Recharts. **15 frontend vitest testi %100 PASS, `npm run build` sıfır hata ile derleniyor!**

---

## 2. 12 Modülün Gerçekleşme Durumu

| Modül | Durum | Tamamlanan Özellikler ve Entegrasyonlar |
|---|---|---|
| **Modül 1: Domain Analysis & SEO Visibility** | TAMAMLANDI | Observed/Provider/Actual panelleri, Organic Research, Keyword Gap, Estimated Traffic Value, Ülke dağılımı, Mobil vs Masaüstü kırılımı, Subdomain analizi, Sitemap & `robots.txt` canlı denetimi, Indexability özeti. |
| **Modül 2: Keyword Research** | TAMAMLANDI | Keyword Magic Tool (soru, long-tail, intent filtreleri, fırsat skoru), Keyword Karşılaştırma Matrisi, CSV dışa aktarımı, SERP/Lexical kümeleme. |
| **Modül 3: Position Tracking** | TAMAMLANDI | İlk 3 / 10 / 20 / 100 dağılımı, Featured Snippet sahiplik takibi, genişletilmiş sıralama alarmları (ilk 10'a giriş, rakip geçişi, sıralama kaybı). |
| **Modül 4: Site Audit** | TAMAMLANDI | 30+ teknik kural, `root_cause`, `seo_impact`, `category` (Frontend, Backend, Infrastructure, Content), durum yönetimi (`open`, `in_progress`, `resolved`, `ignored`), CrUX/PSI, SimHash. |
| **Modül 5: Competitor Intelligence** | TAMAMLANDI | SERP rakip keşfi, Keyword Gap, Backlink Intersect, en az 3 rakibi tek tabloda karşılaştıran **Competitor Comparison Matrix** (organik + ücretli trafik, keyword kesişimi). |
| **Modül 6: Backlink Analytics & Audit** | TAMAMLANDI | Referring IP/Subnet ve TLD dağılımı, link tipleri (`text`/`image`/`nofollow`/`sponsored`/`ugc`), 7 kriterli Toxic Link Audit algoritması ve Disavow `.txt` üretimi. |
| **Modül 7: Content Intelligence & On-Page SEO** | TAMAMLANDI | 13 noktalı On-Page SEO Kontrolcüsü, SEO Content Template, Okunabilirlik (Readability) analizi, Topical Map, Editor Coverage. |
| **Modül 8: Link Building & Outreach** | TAMAMLANDI | Link Prospecting CRM (`link_prospects` tablosu), rakip backlink gap, kırık link tespiti, unlinked brand mentions, onay kapılı e-posta taslak akışı. |
| **Modül 9: PPC Intelligence** | TAMAMLANDI | Rakip ücretli arama kelimeleri (`ppc_keywords`), CPC analizleri, reklam metinleri, Paid Keyword Overlap ve **SEO vs PPC Yamyamlık (Cannibalization)** tespiti. |
| **Modül 10: Social Media Management** | TAMAMLANDI | Çok kanallı sosyal medya yönetimi (X, LinkedIn, Instagram, Facebook), onay kapılı içerik taslak akışı (`draft` -> `approved` -> `published`), etkileşim takibi. |
| **Modül 11: AI SEO Diagnosis & Priority Engine** | TAMAMLANDI | 8 faktörlü matematiksel öncelik skorlama formülü, 4 kategorili sınıflandırma (Frontend, Backend, Infrastructure, Content), Kanban aksiyon panosu (`new`, `planned`, `in_progress`, `completed`, `ignored`). |
| **Modül 12: Deep Code Analysis & Claude Code Fix Agent** | TAMAMLANDI | Claude Code CLI non-interactive köprüsü, Framework tespiti (Next.js, Nuxt, React, FastAPI, Django, WP, HTML), dosya/satır eşleme, unified diff üretimi, **Sıkı Kullanıcı Onay Kapısı** (`user_approved=True`), `.bak` yedeği alma, güvenli yama uygulama ve tek tıkla geri alma (rollback). |
