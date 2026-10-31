# API INTEGRATIONS — Veri Kaynakları ve Entegrasyon Durumu

Platformda sahte veri üretilmez (§2.3). Her veri noktası kendi `source_class` (`FIRST_PARTY`, `OBSERVED`, `PROVIDER`, `MODELED`, `AI`, `DEMO`), toplanma tarihi (`fetched_at`), sağlayıcı adı ve güven seviyesiyle saklanır.

| Entegrasyon / Veri Kaynağı | Amaç | Kaynak Sınıfı | Ortam Değişkenleri (`.env`) | Durum |
|---|---|---|---|---|
| **Claude Code CLI (Doğrudan Subprocess / APIsiz)** | AI SEO Teşhisi, Kök Neden Analizi, Otonom Kod Düzeltme Önerileri, Semantik Katman Verine Sor | `AI` | Yerel binary (`/opt/homebrew/bin/claude` veya `CLAUDE_BIN_PATH`), **Harici API anahtarı gerekmez** | **AKTİF (Doğrudan CLI Entegrasyonu)** |
| **Kendi Web Tarayıcımız (Safe Crawler)** | Teknik Site Denetimi, On-Page SEO, İç Link Grafiği, Sitemap/Robots, Rakip Sayfa Tarama, SERP Brief | `OBSERVED` | Dahili (Anahtar gerektirmez) | **AKTİF (Canlı Çalışıyor)** |
| **Deep Code Analyzer & Fix Agent** | Bağlı yerel proje dizini veya GitHub reposunda kod seviyesi SEO kök neden tespiti ve onaylı yama | `FIRST_PARTY` | Yerel dizin yolu veya `GITHUB_TOKEN` | **AKTİF (Canlı Çalışıyor)** |
| **Common Crawl (WAT & Web Graph)** | Backlink keşfi ve domain otorite (`ds-1`) grafiği | `OBSERVED` | Dahili (`data.commoncrawl.org` akış okuyucu) | **AKTİF (Altyapı Hazır)** |
| **Google Search Console (OAuth2)** | Gerçek tıklama, gösterim, CTR, ortalama pozisyon, URL Inspection, Sitemaps | `FIRST_PARTY` | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | **Altyapı Hazır (Kullanıcı OAuth Bekliyor)** |
| **Google Analytics 4 (Data API)** | Organik oturum, kullanıcı, dönüşüm (kayıt/satış), gelir, sosyal/AI yönlendirme trafiği | `FIRST_PARTY` | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | **Altyapı Hazır (Kullanıcı OAuth Bekliyor)** |
| **Bing Webmaster API** | Bing sorgu performansı ve gelen bağlantılar | `FIRST_PARTY` | Ayarlar → Bing API Key (şifreli saklanır) | **Altyapı Hazır (API Anahtarı Bekliyor)** |
| **Google Ads Keyword Planner API** | Ücretsiz arama hacmi, kelime fikirleri ve rekabet düzeyi | `PROVIDER` | `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_REFRESH_TOKEN` | **Altyapı Hazır (API Anahtarı Bekliyor)** |
| **DataForSEO API** | Canlı SERP, Arama Hacmi, CPC, Ranked Keywords, PPC Reklamları, Backlink İndeksi | `PROVIDER` | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` | **Altyapı Hazır (API Anahtarı Bekliyor)** |
| **CrUX & PageSpeed Insights API** | Gerçek kullanıcı Core Web Vitals (LCP, INP, CLS) ve Lighthouse laboratuvar skorları | `OBSERVED` / `PROVIDER` | `CRUX_API_KEY` | **Altyapı Hazır (API Anahtarı Bekliyor)** |
| **Sosyal Medya Platform API'leri** | LinkedIn, X, Instagram, Facebook hesap bağlantısı ve onaylı yayınlama | `FIRST_PARTY` | Hesap bazlı OAuth token | **Altyapı Hazır (OAuth Token Bekliyor)** |
| **DEMO / Sandbox Sağlayıcı (`MockProvider`)** | Dış ücretli API anahtarları girilmeden tüm akışları deterministik olarak test etme | `DEMO` (`is_synthetic=True`) | Üst bardan tek tıkla açılır/kapatılır | **AKTİF (Açıkça `DEMO` Etiketli)** |
