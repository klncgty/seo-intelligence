import pytest

from app.services.tag_intelligence import analyze_page_tags, compare_tags, run_tag_intelligence
from tests.conftest import html

HTML_TARGET = f"""<!DOCTYPE html>
<html>
<head>
    <title>Oleart Zeytinyağı</title>
    <script type="application/ld+json">{{"@type":"Organization","name":"Oleart"}}</script>
</head>
<body>
    <h1>Zeytinyağı</h1>
    <p>Trilye bahçemizden doğal zeytinyağı üretimi ve satışı.</p>
</body>
</html>"""

HTML_COMP_A = f"""<!DOCTYPE html>
<html>
<head>
    <title>Soğuk Sıkım Naturel Sızma Zeytinyağı — 2026 Erken Hasat</title>
    <meta name="description" content="Erken hasat soğuk sıkım naturel sızma zeytinyağı doğrudan üreticisinden en uygun fiyat ve ücretsiz kargo ile kapınızda. Hemen satın alın.">
    <script type="application/ld+json">[{{"@type":"Product","name":"Naturel Sızma Zeytinyağı"}},{{"@type":"FAQPage"}}]</script>
</head>
<body>
    <h1>Soğuk Sıkım Naturel Sızma Zeytinyağı</h1>
    <h2>Zeytinyağı Nasıl Saklanır?</h2>
    <h2>Asit Oranı ve Kalite Belirteçleri</h2>
    <h2>Erken Hasat Zeytinyağı Fiyatları</h2>
    <p>Soğuk sıkım zeytinyağı asit polifenol trilye erken hasat naturel sızma filtre edilmemiş cam şişe teneke.</p>
    <img src="/img1.jpg" alt="soğuk sıkım zeytinyağı teneke">
</body>
</html>"""

HTML_COMP_B = f"""<!DOCTYPE html>
<html>
<head>
    <title>Erken Hasat Soğuk Sıkım Zeytinyağı Fiyatı ve Çeşitleri</title>
    <meta name="description" content="Doğal filtre edilmemiş soğuk sıkım zeytinyağı çeşitleri ve analiz raporlu fiyatları. Katkısız lezzeti keşfedin.">
    <script type="application/ld+json">{{"@type":"Product","name":"Zeytinyağı"}}</script>
</head>
<body>
    <h1>En İyi Soğuk Sıkım Zeytinyağı</h1>
    <h2>Zeytinyağı Asit Oranı Nedir?</h2>
    <h2>Zeytinyağı Nasıl Saklanır?</h2>
    <p>Zeytinyağı erken hasat polifenol filtre soğuk sıkım şişe naturel sızma asit oranı analiz raporlu.</p>
</body>
</html>"""


def test_analyze_page_tags():
    analysis = analyze_page_tags(HTML_COMP_A.encode("utf-8"), "https://rakip-a.com/zeytinyagi", "tr")
    assert "Soğuk Sıkım" in analysis["title"]
    assert "Erken hasat" in analysis["meta_description"]
    assert "Product" in analysis["schema_types"]
    assert "FAQPage" in analysis["schema_types"]
    assert len(analysis["h2"]) >= 3
    assert analysis["images_count"] == 1
    assert analysis["images_missing_alt"] == 0
    assert "polifenol" in analysis["terms"]


def test_compare_tags_and_blueprint():
    t_analysis = analyze_page_tags(HTML_TARGET.encode("utf-8"), "https://oleart.co/", "tr")
    c_analyses = [
        analyze_page_tags(HTML_COMP_A.encode("utf-8"), "https://rakip-a.com/zeytinyagi", "tr"),
        analyze_page_tags(HTML_COMP_B.encode("utf-8"), "https://rakip-b.com/zeytinyagi", "tr"),
    ]

    result = compare_tags(t_analysis, c_analyses, "soğuk sıkım zeytinyağı", "tr")

    assert result["competitors_analyzed"] == 2
    assert "Product" in [s["schema"] for s in result["gaps"]["missing_schemas"]]
    # check that questions asked by competitors are detected
    assert len(result["competitor_benchmarks"]["competitor_questions"]) >= 1
    # check blueprint
    bp = result["winning_tag_blueprint"]
    assert "soğuk sıkım zeytinyağı" in bp["recommended_title"].lower()
    assert len(bp["recommended_h2_headings"]) >= 3
    assert "Product" in bp["recommended_schema_types"]
    assert "FAQPage" in bp["recommended_schema_types"]
    assert len(bp["must_have_focus_terms"]) >= 5


def test_api_tag_intelligence_with_fixtures(client, fixture_site, settings):
    settings(crawler_allow_private_networks=True)
    comp_a = fixture_site({"/zeytin": html("Rakip A", HTML_COMP_A)})
    comp_b = fixture_site({"/zeytin": html("Rakip B", HTML_COMP_B)})
    target = fixture_site({"/": html("Target", HTML_TARGET)})

    payload = {
        "url": f"{target.base}/",
        "competitor_urls": [f"{comp_a.base}/zeytin", f"{comp_b.base}/zeytin"],
        "keyword": "soğuk sıkım zeytinyağı",
        "lang": "tr",
    }
    res = client.post("/api/v1/content/tag-intelligence", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["competitors_analyzed"] == 2
    assert data["target_keyword"] == "soğuk sıkım zeytinyağı"
    assert "winning_tag_blueprint" in data
    assert len(data["winning_tag_blueprint"]["must_have_focus_terms"]) > 0
