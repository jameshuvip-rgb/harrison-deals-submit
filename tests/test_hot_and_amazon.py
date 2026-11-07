"""Hot Deals ranking and Amazon link fill. No live Amazon calls."""
from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
os.environ["HD_PERSIST_ROOT"] = tempfile.mkdtemp(prefix="hd-test-")
os.environ["SECRET_KEY"] = "test-secret"
os.environ.pop("HD_PUBLIC_BASE", None)

import app as hd  # noqa: E402

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SAMPLE_HTML = """
<html><head>
<meta property="og:title" content="Ignored title : Amazon.ca" />
</head><body>
<span id="productTitle">  Honda Cabin Filter Kit  </span>
<script>
"priceToPay":{"priceAmount":19.99,"currency":"CAD"}
"basisPrice":{"priceAmount":34.99}
"hiRes":"https://m.media-amazon.com/images/I/example.jpg"
</script>
</body></html>
"""


def card(deal_id, off, posted, views=0, fav=0, image="/static/x.jpg"):
    return {
        "id": deal_id,
        "track_key": f"seed:{deal_id}",
        "source": "seed",
        "off": off,
        "posted": posted,
        "views_30m": views,
        "favorites": fav,
        "image": image,
        "title": deal_id,
        "price": 10,
        "was": 20,
    }


def feat_ids(html: str) -> list[str]:
    return re.findall(r'class="feat-card"[^>]*data-id="([^"]+)"', html)


class HotRankTests(unittest.TestCase):
    def test_fallback_is_biggest_discount_then_latest_max_three(self):
        deals = [
            card("low", 10, 100),
            card("tie-old", 40, 1),
            card("tie-new", 40, 5),
            card("no-image", 90, 9, image=""),
        ]
        picked = hd.pick_hot_deals(deals)
        self.assertEqual([d["id"] for d in picked], ["tie-new", "tie-old", "low"])
        self.assertEqual(picked[0]["hot_label"], "Biggest % off")
        self.assertEqual(picked[1]["hot_label"], "Biggest % off")
        self.assertEqual(picked[2]["hot_label"], "Latest")

    def test_three_slots_skip_a_winner_already_used(self):
        deals = [
            card("viewer", 10, 1, views=8, fav=9),
            card("deal", 50, 2, views=1, fav=1),
            card("saver", 20, 3, fav=4),
            card("other", 15, 4, fav=2),
        ]
        picked = hd.pick_hot_deals(deals)
        self.assertEqual([d["id"] for d in picked], ["viewer", "deal", "saver"])
        self.assertEqual(
            [d["hot_label"] for d in picked],
            ["Most viewed", "Biggest % off", "Most saved"],
        )

    def test_views_slot_then_discount_among_the_rest(self):
        deals = [
            card("viewed", 10, 1, views=4),
            card("best-off", 50, 2, views=1),
            card("also", 40, 9),
        ]
        picked = hd.pick_hot_deals(deals)
        self.assertEqual([d["id"] for d in picked], ["viewed", "best-off", "also"])
        self.assertEqual(
            [d["hot_label"] for d in picked],
            ["Most viewed", "Biggest % off", "Latest"],
        )

    def test_view_tie_breaks_on_favorites_then_updated(self):
        deals = [
            card("older-fave", 10, 1, views=3, fav=2),
            card("newer-plain", 80, 9, views=3, fav=0),
            card("same-views-more-faves", 10, 2, views=3, fav=5),
        ]
        picked = hd.pick_hot_deals(deals)
        self.assertEqual(picked[0]["id"], "same-views-more-faves")
        self.assertEqual(picked[1]["id"], "newer-plain")
        self.assertEqual(picked[1]["hot_label"], "Biggest % off")

    def test_equal_favorites_use_updated_at(self):
        deals = [
            card("older", 15, 1, views=2, fav=4),
            card("newer", 15, 8, views=2, fav=4),
        ]
        picked = hd.pick_hot_deals(deals)
        self.assertEqual(picked[0]["id"], "newer")

    def test_empty_discount_slot_uses_favorites(self):
        deals = [
            card("hot", 0, 1, views=6),
            card("saved", 0, 2, fav=3),
            card("fresh", 0, 9, fav=3),
        ]
        picked = hd.pick_hot_deals(deals)
        self.assertEqual([d["id"] for d in picked], ["hot", "fresh", "saved"])
        self.assertEqual(picked[0]["hot_label"], "Most viewed")
        self.assertEqual(picked[1]["hot_label"], "Most saved")
        self.assertEqual(picked[2]["hot_label"], "Latest")

    def test_single_deal_returns_one(self):
        picked = hd.pick_hot_deals([card("only", 5, 1)])
        self.assertEqual(len(picked), 1)

    def test_cap_is_three_even_if_caller_asks_for_more(self):
        deals = [card(str(i), i, i) for i in range(5)]
        picked = hd.pick_hot_deals(deals, 5)
        self.assertEqual([d["id"] for d in picked], ["4", "3", "2"])


class AmazonParseTests(unittest.TestCase):
    def test_product_urls(self):
        cases = {
            "https://www.amazon.ca/dp/B0HHJ93L1P": ("B0HHJ93L1P", "ca"),
            "https://www.amazon.ca/Toyota-Filter/dp/B0HHJ93L1P/ref=sr_1_1": ("B0HHJ93L1P", "ca"),
            "https://www.amazon.com/gp/product/B0HJQDLJK3": ("B0HJQDLJK3", "com"),
            "https://www.amazon.ca/gp/aw/d/b0hjyjwjvY": ("B0HJYJWJVY", "ca"),
            "https://smile.amazon.com/dp/B0HKH5HMW3?tag=x": ("B0HKH5HMW3", "com"),
            "http://amazon.ca/dp/B0HHJ93L1P?asin=IGNORED000": ("B0HHJ93L1P", "ca"),
        }
        for url, expected in cases.items():
            self.assertEqual(hd.amazon_marketplace(url), expected, url)

    def test_rejects_non_product_links(self):
        for url in (
            "https://example.com/dp/B0HHJ93L1P",
            "https://www.amazon.ca/s?k=filter",
            "https://www.amazon.ca/dp/TOOSHORT",
            "https://notamazon.com/dp/B0HHJ93L1P",
            "",
        ):
            self.assertIsNone(hd.amazon_marketplace(url), url)

    def test_html_extracts_title_price_and_image(self):
        info = hd.parse_amazon_html(SAMPLE_HTML)
        self.assertFalse(info["blocked"])
        self.assertEqual(info["title"], "Honda Cabin Filter Kit")
        self.assertEqual(info["deal_price"], 19.99)
        self.assertEqual(info["list_price"], 34.99)
        self.assertEqual(info["image_url"], "https://m.media-amazon.com/images/I/example.jpg")

    def test_captcha_fails_soft(self):
        info = hd.parse_amazon_html("<html>sorry, we just need to make sure you're not a robot validateCaptcha</html>")
        self.assertTrue(info["blocked"])
        self.assertIsNone(info["title"])
        self.assertIsNone(info["deal_price"])
        self.assertIsNone(info["image_url"])

    def test_buy_box_price_ignores_later_recommendation(self):
        html = """
        <span id="productTitle">Cabin Filter</span>
        <div id="corePrice_feature_div">
          <span class="a-price apex-pricetopay-value">
            <span class="a-offscreen">$29.99</span>
            <span class="a-price-whole">29</span><span class="a-price-fraction">99</span>
          </span>
        </div>
        <div id="corePriceDisplay_desktop_feature_div">
          <span class="a-price"><span class="a-offscreen">$17.09</span></span>
        </div>
        """
        info = hd.parse_amazon_html(html)
        self.assertEqual(info["deal_price"], 29.99)
        self.assertIsNone(info["list_price"])

    def test_list_price_not_above_deal_is_dropped(self):
        html = '"priceToPay":{"priceAmount":20.00} "basisPrice":{"priceAmount":15.00}'
        info = hd.parse_amazon_html(html)
        self.assertEqual(info["deal_price"], 20.0)
        self.assertIsNone(info["list_price"])

    def test_landing_image_prefers_hires_over_carousel(self):
        html = """
        <img id="landingImage"
             src="https://m.media-amazon.com/images/I/71main._AC_SX679_.jpg"
             data-old-hires="https://m.media-amazon.com/images/I/71main._AC_SL1500_.jpg"
             data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/71main._AC_SX466_.jpg&quot;:[466,466],&quot;https://m.media-amazon.com/images/I/71main._AC_SX679_.jpg&quot;:[679,679]}" />
        "hiRes":"https://m.media-amazon.com/images/I/81other._AC_UL2000_.jpg"
        <meta property="og:image" content="https://m.media-amazon.com/images/I/71main._AC_SX522_.jpg" />
        """
        info = hd.parse_amazon_html(html)
        self.assertEqual(
            info["image_url"],
            "https://m.media-amazon.com/images/I/71main._AC_SL1500_.jpg",
        )
        self.assertLessEqual(len(info["image_candidates"]), 2)
        self.assertEqual(info["image_candidates"][0], info["image_url"])

    def test_small_landing_image_upgrades_to_sl1500(self):
        html = """
        <img id="landingImage"
             data-a-dynamic-image="{&quot;https://m.media-amazon.com/images/I/71dyn._AC_SX466_.jpg&quot;:[466,466],&quot;https://m.media-amazon.com/images/I/71dyn._AC_SX679_.jpg&quot;:[679,679]}"
             src="https://m.media-amazon.com/images/I/71dyn._AC_SX466_.jpg" />
        """
        info = hd.parse_amazon_html(html)
        self.assertEqual(
            info["image_url"],
            "https://m.media-amazon.com/images/I/71dyn._AC_SL1500_.jpg",
        )
        self.assertIn(
            "https://m.media-amazon.com/images/I/71dyn._AC_SX679_.jpg",
            info["image_candidates"],
        )

    def test_escaped_hires_and_og_image_and_foreign_host(self):
        escaped = hd.parse_amazon_html(
            r'"hiRes":"https:\/\/m.media-amazon.com\/images\/I\/71esc._AC_SL1500_.jpg"'
        )
        self.assertEqual(
            escaped["image_url"],
            "https://m.media-amazon.com/images/I/71esc._AC_SL1500_.jpg",
        )
        og = hd.parse_amazon_html(
            '<meta content="https://m.media-amazon.com/images/I/71og._AC_SL1500_.jpg" property="og:image" />'
        )
        self.assertEqual(
            og["image_url"],
            "https://m.media-amazon.com/images/I/71og._AC_SL1500_.jpg",
        )
        foreign = hd.parse_amazon_html(
            '"hiRes":"https://evil.example/images/I/71nope._AC_SL1500_.jpg" '
            '<meta property="og:image" content="https://m.media-amazon.com/images/I/71ok.jpg" />'
        )
        self.assertEqual(foreign["image_url"], "https://m.media-amazon.com/images/I/71ok.jpg")

    def test_product_identity_uses_asin_or_url(self):
        asins, markets = hd.product_identity(
            "b0abcdefgh",
            "https://www.amazon.ca/Name/dp/B0ABCDEFGH/ref=sr_1_1?tag=x",
        )
        self.assertEqual(asins, {"B0ABCDEFGH"})
        self.assertEqual(markets, {("ca", "B0ABCDEFGH")})
        asins, markets = hd.product_identity(
            "B0AAAAAAAA",
            "https://www.amazon.com/gp/product/B0BBBBBBBB",
        )
        self.assertEqual(asins, {"B0AAAAAAAA", "B0BBBBBBBB"})
        self.assertEqual(markets, {("com", "B0BBBBBBBB")})


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        hd.app.config["TESTING"] = True
        db = sqlite3.connect(hd.DB_PATH)
        db.execute("DELETE FROM deal_views")
        db.execute("DELETE FROM deal_favorites")
        db.execute("DELETE FROM deals")
        db.execute("DELETE FROM users")
        db.commit()
        db.close()
        self.client = hd.app.test_client()
        self._inbox_before = {path.name for path in hd.INBOX.glob("*.json")}

    def tearDown(self):
        for path in hd.INBOX.glob("*.json"):
            if path.name not in self._inbox_before:
                path.unlink()

    def _html(self, path: str) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def _register(self, email="seller@example.com", client=None):
        client = client or self.client
        response = client.post(
            "/register",
            data={"email": email, "password": "password123", "confirm": "password123"},
        )
        self.assertIn(response.status_code, {302, 303})
        return client

    def test_home_fallback_shows_two_imaged_discount_leaders(self):
        html = self._html("/")
        self.assertEqual(html.count('class="feat-card"'), 3)
        self.assertIn(".feat-card .thumb { width:100%; height:190px;", html)
        self.assertIn("Today's pick", html)
        self.assertNotIn("height:148px", html)
        self.assertNotIn("hot-pill", html)
        self.assertEqual(feat_ids(html), ["B0HC3N5BHW", "B0HF7Q25KL", "B0HF81MTRH"])
        self.assertIn(">Save<", html)

    def test_search_hides_hot_deals_and_category_stays_in_pool(self):
        search = self._html("/?q=tesla")
        self.assertEqual(search.count('class="feat-card"'), 0)
        self.assertEqual(search.count("Today's pick"), 0)
        self.assertIn("Tesla", search)

        sports = self._html("/?cat=sports")
        self.assertEqual(feat_ids(sports), ["B0HF7Q25KL", "B0HF81MTRH", "B0HF81MWNT"])

        home = self._html("/?cat=home")
        self.assertEqual(feat_ids(home), ["B0HC3N5BHW"])

        pets = self._html("/?cat=pets")
        self.assertEqual(pets.count('class="feat-card"'), 0)

    def test_view_is_debounced_and_ranks_slot_one(self):
        first = self.client.post("/api/deals/view", json={"track_key": "seed:B0HHJ93L1P"})
        second = self.client.post("/api/deals/view", json={"track_key": "seed:B0HHJ93L1P"})
        self.assertTrue(first.get_json()["counted"])
        self.assertFalse(second.get_json()["counted"])
        html = self._html("/")
        ids = feat_ids(html)
        self.assertEqual(ids, ["B0HHJ93L1P", "B0HC3N5BHW", "B0HF7Q25KL"])
        self.assertIn("Today's pick", html)

        other = hd.app.test_client()
        again = other.post(
            "/api/deals/view",
            json={"track_key": "seed:B0HHJ93L1P"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        # Different session, but the first client was 127.0.0.1, so this IP counts.
        self.assertTrue(again.get_json()["counted"])
        same_ip = hd.app.test_client()
        skipped = same_ip.post(
            "/api/deals/view",
            json={"track_key": "seed:B0HHJ93L1P"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        self.assertFalse(skipped.get_json()["counted"])

    def test_stale_views_do_not_count_and_unknown_keys_404(self):
        now = int(time.time())
        db = sqlite3.connect(hd.DB_PATH)
        db.execute(
            "INSERT INTO deal_views (deal_key, session_key, ip_key, viewed_at) VALUES (?, ?, ?, ?)",
            ("seed:B0HHJ93L1P", "s:old", "i:old", now - hd.VIEW_WINDOW_SEC - 60),
        )
        db.commit()
        db.close()
        html = self._html("/")
        self.assertEqual(feat_ids(html), ["B0HC3N5BHW", "B0HF7Q25KL", "B0HF81MTRH"])
        missing = self.client.post("/api/deals/view", json={"track_key": "seed:NOTADEAL"})
        self.assertEqual(missing.status_code, 404)
        blocked = self.client.post(
            "/api/deals/view",
            json={"track_key": "seed:B0HHJ93L1P"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(blocked.status_code, 400)

    def test_favorite_requires_login_and_breaks_view_ties(self):
        guest = self.client.post("/api/deals/favorite", json={"track_key": "seed:B0HF7Q25KL"})
        self.assertEqual(guest.status_code, 401)

        self._register()
        saved = self.client.post("/api/deals/favorite", json={"track_key": "seed:B0HF7Q25KL"})
        body = saved.get_json()
        self.assertTrue(body["favorited"])
        self.assertEqual(body["favorites"], 1)
        html = self._html("/")
        self.assertIn('data-track="seed:B0HF7Q25KL"', html)
        self.assertIn('data-favorited="1"', html)
        self.assertIn('data-favorites="1"', html)

        off = self.client.post("/api/deals/favorite", json={"track_key": "seed:B0HF7Q25KL"})
        self.assertFalse(off.get_json()["favorited"])
        self.assertEqual(off.get_json()["favorites"], 0)

        now = int(time.time())
        db = sqlite3.connect(hd.DB_PATH)
        for key in ("seed:B0HC3N5BHW", "seed:B0HF7Q25KL"):
            db.execute(
                "INSERT INTO deal_views (deal_key, session_key, ip_key, viewed_at) VALUES (?, ?, ?, ?)",
                (key, "s:" + key, "i:" + key, now),
            )
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            db.execute(
                "INSERT INTO deal_favorites (deal_key, user_email, created_at) VALUES (?, ?, ?)",
                ("seed:B0HF7Q25KL", email, hd.utc_now()),
            )
        db.commit()
        db.close()
        html = self._html("/")
        self.assertEqual(feat_ids(html)[0], "B0HF7Q25KL")
        self.assertIn('data-favorites="3"', html)

    def test_user_deal_is_eligible(self):
        self._register()
        response = self.client.post(
            "/post",
            data={
                "title": "Seller posted filter",
                "asin": "B0USERHOT1",
                "amazon_url": "https://www.amazon.ca/dp/B0USERHOT1",
                "deal_price": "40.00",
                "list_price": "44.00",
                "department": "Electronics",
                "cover": (io.BytesIO(TINY_PNG), "cover.png"),
            },
        )
        self.assertEqual(response.status_code, 302)
        db = sqlite3.connect(hd.DB_PATH)
        deal_id = db.execute("SELECT id FROM deals").fetchone()[0]
        db.close()
        other = hd.app.test_client()
        viewed = other.post("/api/deals/view", json={"track_key": f"user:{deal_id}"})
        self.assertTrue(viewed.get_json()["counted"])
        html = self._html("/")
        self.assertEqual(feat_ids(html)[0], "B0USERHOT1")

        stranger = hd.app.test_client()
        self._register("other@example.com", stranger)
        forbidden = stranger.post(f"/deals/{deal_id}/edit", data={"title": "hijack", "deal_price": "1"})
        self.assertEqual(forbidden.status_code, 403)

    def test_lookup_fail_soft_and_success_can_post_without_upload(self):
        guest = self.client.post("/api/amazon-lookup", json={"url": "https://www.amazon.ca/dp/B0HHJ93L1P"})
        self.assertEqual(guest.status_code, 401)
        self._register()

        calls = []

        def no_html(url, accept_language):
            calls.append(url)
            return None

        def no_image(url, referer=None):
            raise AssertionError(url)

        original_html = hd.fetch_amazon_html
        original_image = hd.fetch_amazon_image
        hd.fetch_amazon_html = no_html
        hd.fetch_amazon_image = no_image
        try:
            bad = self.client.post("/api/amazon-lookup", json={"url": "https://example.com/dp/B0LOOKUP01"})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(calls, [])
            soft = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.com/Some-Name/dp/B0LOOKUP01?ref=abc"},
            )
            payload = soft.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["asin"], "B0LOOKUP01")
            self.assertIsNone(payload["deal_price"])
            self.assertFalse(payload["duplicate"])
            self.assertIn(hd.MSG_NO_PRICE, payload["messages"])
            self.assertIn(hd.MSG_NO_COVER, payload["messages"])
            self.assertNotIn(hd.MSG_DUPLICATE, payload["messages"])
            self.assertEqual(calls, ["https://www.amazon.com/dp/B0LOOKUP01"])

            def good_html(url, accept_language):
                calls.append((url, accept_language))
                return SAMPLE_HTML

            def good_image(url, referer=None):
                self.assertTrue(url.startswith("https://m.media-amazon.com/"))
                self.assertTrue(referer)
                return TINY_PNG

            hd.fetch_amazon_html = good_html
            hd.fetch_amazon_image = good_image
            filled = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.ca/dp/B0LOOKUP01"},
            )
            body = filled.get_json()
            self.assertEqual(body["messages"], [hd.MSG_FILLED])
            self.assertFalse(body["duplicate"])
            self.assertEqual(body["title"], "Honda Cabin Filter Kit")
            self.assertEqual(body["deal_price"], 19.99)
            self.assertEqual(body["list_price"], 34.99)
            self.assertTrue(body["cover_token"])
            self.assertEqual(calls[-1][0], "https://www.amazon.ca/dp/B0LOOKUP01")
            self.assertIn("en-CA", calls[-1][1])
        finally:
            hd.fetch_amazon_html = original_html
            hd.fetch_amazon_image = original_image

        token = body["cover_token"]
        posted = self.client.post(
            "/post",
            data={
                "title": body["title"],
                "asin": body["asin"],
                "amazon_url": "https://www.amazon.ca/dp/B0LOOKUP01",
                "deal_price": "19.99",
                "list_price": "34.99",
                "department": "Automotive",
                "scraped_cover": token,
            },
        )
        self.assertEqual(posted.status_code, 302)
        db = sqlite3.connect(hd.DB_PATH)
        row = db.execute("SELECT title, asin, deal_price, list_price, cover_path FROM deals").fetchone()
        db.close()
        self.assertEqual(row[0], "Honda Cabin Filter Kit")
        self.assertEqual(row[1], "B0LOOKUP01")
        self.assertEqual(row[2], 19.99)
        self.assertEqual(row[3], 34.99)
        self.assertTrue(row[4])
        self.assertTrue((hd.UPLOADS / row[4]).is_file())

        page = self._html("/post")
        self.assertIn("amazon-fill.js", page)
        self.assertIn("data-amazon-status", page)
        self.assertIn('name="cover"', page)
        self.assertIn('name="scraped_cover"', page)

    def test_uploaded_file_wins_over_scraped_token(self):
        self._register()
        db = sqlite3.connect(hd.DB_PATH)
        user_id = db.execute("SELECT id FROM users").fetchone()[0]
        db.close()
        folder = hd.UPLOADS / "_lookup" / str(user_id)
        folder.mkdir(parents=True, exist_ok=True)
        token = "ab" * 16
        (folder / f"{token}.png").write_bytes(TINY_PNG)
        uploaded = TINY_PNG + b"UPLOAD"
        response = self.client.post(
            "/post",
            data={
                "title": "File wins",
                "asin": "B0FILEWINS",
                "deal_price": "9.00",
                "department": "Tools",
                "scraped_cover": token,
                "cover": (io.BytesIO(uploaded), "mine.png"),
            },
        )
        self.assertEqual(response.status_code, 302)
        db = sqlite3.connect(hd.DB_PATH)
        cover = db.execute("SELECT cover_path FROM deals").fetchone()[0]
        db.close()
        self.assertTrue((hd.UPLOADS / cover).read_bytes().endswith(b"UPLOAD"))
        self.assertTrue((folder / f"{token}.png").is_file())

    def _post_deal(self, asin="", amazon_url=None, title="Seller filter", client=None, **extra):
        client = client or self.client
        if amazon_url is None and asin:
            amazon_url = f"https://www.amazon.ca/dp/{asin}"
        data = {
            "title": title,
            "asin": asin,
            "amazon_url": amazon_url or "",
            "deal_price": "12.00",
            "list_price": "20.00",
            "department": "Automotive",
            "cover": (io.BytesIO(TINY_PNG), "cover.png"),
        }
        data.update(extra)
        return client.post("/post", data=data)

    def _deal_id(self) -> int:
        db = sqlite3.connect(hd.DB_PATH)
        deal_id = db.execute("SELECT id FROM deals ORDER BY id DESC").fetchone()[0]
        db.close()
        return int(deal_id)

    def test_duplicate_asin_and_normalized_url_are_rejected(self):
        self._register()
        first = self._post_deal("B0DUPASIN1", title="First listing")
        self.assertEqual(first.status_code, 302)

        again = self._post_deal("b0dupasin1", title="Same ASIN different case")
        self.assertEqual(again.status_code, 200)
        self.assertIn(hd.MSG_DUPLICATE, again.get_data(as_text=True))

        other_market = self._post_deal(
            "B0DUPASIN1",
            amazon_url="https://www.amazon.com/dp/B0DUPASIN1",
            title="Same ASIN on dot com",
        )
        self.assertIn(hd.MSG_DUPLICATE, other_market.get_data(as_text=True))

        shaped = self._post_deal(
            "",
            amazon_url="https://www.amazon.ca/Some-Long-Name/gp/product/B0DUPASIN1/ref=sr_1_1?tag=x",
            title="Same product, different URL text",
        )
        self.assertIn(hd.MSG_DUPLICATE, shaped.get_data(as_text=True))

        db = sqlite3.connect(hd.DB_PATH)
        db.execute("UPDATE deals SET asin = ? WHERE asin = ?", ("b0dupasin1", "B0DUPASIN1"))
        db.commit()
        db.close()
        stranger = hd.app.test_client()
        self._register("other@example.com", stranger)
        lowered = self._post_deal("B0DUPASIN1", title="Stored lowercase ASIN", client=stranger)
        self.assertIn(hd.MSG_DUPLICATE, lowered.get_data(as_text=True))

        fresh = self._post_deal("B0DUPASIN2", title="Different product", client=stranger)
        self.assertEqual(fresh.status_code, 302)

        db = sqlite3.connect(hd.DB_PATH)
        count = db.execute("SELECT COUNT(*) FROM deals WHERE deleted_at IS NULL").fetchone()[0]
        db.close()
        self.assertEqual(count, 2)

    def test_deleted_deal_asin_can_be_posted_again(self):
        self._register()
        self.assertEqual(self._post_deal("B0REPOST01").status_code, 302)
        deal_id = self._deal_id()
        removed = self.client.post(f"/deals/{deal_id}/delete")
        self.assertEqual(removed.status_code, 302)
        again = self._post_deal("B0REPOST01", title="Reposted after delete")
        self.assertEqual(again.status_code, 302)
        db = sqlite3.connect(hd.DB_PATH)
        live = db.execute(
            "SELECT COUNT(*) FROM deals WHERE asin = ? AND deleted_at IS NULL",
            ("B0REPOST01",),
        ).fetchone()[0]
        db.close()
        self.assertEqual(live, 1)

    def test_seed_asin_is_rejected_until_the_seed_is_removed(self):
        self._register()
        blocked = self._post_deal("B0HHJ93L1P", title="Copy of a catalog deal")
        self.assertEqual(blocked.status_code, 200)
        self.assertIn(hd.MSG_DUPLICATE, blocked.get_data(as_text=True))
        url_only = self._post_deal(
            "",
            amazon_url="https://www.amazon.ca/Toyota-Filter/dp/B0HHJ93L1P?ref=1",
            title="Catalog link without an ASIN field",
        )
        self.assertIn(hd.MSG_DUPLICATE, url_only.get_data(as_text=True))
        db = sqlite3.connect(hd.DB_PATH)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM deals").fetchone()[0], 0)
        db.close()

        try:
            hd.save_deleted_seed_ids({"B0HHJ93L1P"})
            allowed = self._post_deal("B0HHJ93L1P", title="Seed was taken down")
            self.assertEqual(allowed.status_code, 302)
        finally:
            hd.save_deleted_seed_ids(set())

    def test_edit_keeps_same_asin_and_blocks_a_different_product(self):
        self._register()
        self.assertEqual(self._post_deal("B0EDITKEEP", title="Original title").status_code, 302)
        deal_id = self._deal_id()
        self.assertEqual(self._post_deal("B0EDITOTH1", title="Other product").status_code, 302)

        kept = self.client.post(
            f"/deals/{deal_id}/edit",
            data={
                "title": "Renamed but same product",
                "asin": "b0editkeep",
                "amazon_url": "https://www.amazon.ca/Renamed/gp/product/B0EDITKEEP/ref=abc",
                "deal_price": "14.50",
                "list_price": "20.00",
                "department": "Automotive",
            },
        )
        self.assertEqual(kept.status_code, 302)
        db = sqlite3.connect(hd.DB_PATH)
        title, asin = db.execute(
            "SELECT title, asin FROM deals WHERE id = ?",
            (deal_id,),
        ).fetchone()
        db.close()
        self.assertEqual(title, "Renamed but same product")
        self.assertEqual(asin, "B0EDITKEEP")

        conflict = self.client.post(
            f"/deals/{deal_id}/edit",
            data={
                "title": "Should not stick",
                "asin": "B0EDITOTH1",
                "amazon_url": "https://www.amazon.com/dp/B0EDITOTH1",
                "deal_price": "9.00",
                "department": "Tools",
            },
        )
        self.assertEqual(conflict.status_code, 200)
        self.assertIn(hd.MSG_DUPLICATE, conflict.get_data(as_text=True))
        db = sqlite3.connect(hd.DB_PATH)
        title, asin, price = db.execute(
            "SELECT title, asin, deal_price FROM deals WHERE id = ?",
            (deal_id,),
        ).fetchone()
        db.close()
        self.assertEqual(title, "Renamed but same product")
        self.assertEqual(asin, "B0EDITKEEP")
        self.assertEqual(price, 14.5)

        seed_swap = self.client.post(
            f"/deals/{deal_id}/edit",
            data={
                "title": "Seed grab",
                "asin": "B0HF7Q25KL",
                "amazon_url": "https://www.amazon.ca/dp/B0HF7Q25KL",
                "deal_price": "8.00",
                "department": "Sports",
            },
        )
        self.assertIn(hd.MSG_DUPLICATE, seed_swap.get_data(as_text=True))
        db = sqlite3.connect(hd.DB_PATH)
        self.assertEqual(
            db.execute("SELECT asin FROM deals WHERE id = ?", (deal_id,)).fetchone()[0],
            "B0EDITKEEP",
        )
        db.close()

    def test_deals_without_an_amazon_identity_are_not_duplicates(self):
        self._register()
        first = self._post_deal("", amazon_url="", title="No link one")
        second = self._post_deal("", amazon_url="", title="No link two")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)

    def test_api_submit_rejects_duplicate_asin(self):
        self._register()
        payload = {
            "title": "API deal",
            "asin": "B0APIDUP01",
            "amazon_url": "https://www.amazon.ca/dp/B0APIDUP01",
            "deal_price": "5.00",
        }
        first = self.client.post("/api/submit", json=payload)
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/submit", json=payload)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.get_json()["error"], hd.MSG_DUPLICATE)

    def test_lookup_warns_on_duplicate_but_still_fills(self):
        self._register()
        self.assertEqual(self._post_deal("B0WARNAS01", title="Already live").status_code, 302)
        deal_id = self._deal_id()

        def good_html(url, accept_language):
            return SAMPLE_HTML

        def image_calls(url, referer=None):
            image_calls.urls.append(url)
            if url.endswith("._AC_SL1500_.jpg"):
                return None
            return TINY_PNG

        image_calls.urls = []
        original_html = hd.fetch_amazon_html
        original_image = hd.fetch_amazon_image
        hd.fetch_amazon_html = good_html
        hd.fetch_amazon_image = image_calls
        try:
            warned = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.ca/Different-Text/dp/B0WARNAS01?ref=1"},
            )
            body = warned.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["asin"], "B0WARNAS01")
            self.assertTrue(body["duplicate"])
            self.assertIn(hd.MSG_DUPLICATE, body["messages"])
            self.assertEqual(body["title"], "Honda Cabin Filter Kit")

            own = self.client.post(
                "/api/amazon-lookup",
                json={
                    "url": "https://www.amazon.ca/dp/B0WARNAS01",
                    "deal_id": deal_id,
                },
            )
            own_body = own.get_json()
            self.assertFalse(own_body["duplicate"])
            self.assertNotIn(hd.MSG_DUPLICATE, own_body["messages"])

            stranger = hd.app.test_client()
            self._register("third@example.com", stranger)
            hidden = stranger.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.ca/dp/B0WARNAS01", "deal_id": deal_id},
            )
            self.assertTrue(hidden.get_json()["duplicate"])

            catalog = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.ca/dp/B0HHJ93L1P"},
            )
            self.assertTrue(catalog.get_json()["duplicate"])
            self.assertEqual(catalog.get_json()["asin"], "B0HHJ93L1P")
        finally:
            hd.fetch_amazon_html = original_html
            hd.fetch_amazon_image = original_image

    def test_cover_fetch_falls_back_once_when_largest_url_fails(self):
        self._register()
        page = """
        <span id="productTitle">Fallback Cover</span>
        <img id="landingImage" src="https://m.media-amazon.com/images/I/71fall._AC_SX679_.jpg" />
        "priceToPay":{"priceAmount":11.00}
        """

        def good_html(url, accept_language):
            good_html.calls.append(url)
            return page

        def flaky_image(url, referer=None):
            flaky_image.urls.append(url)
            if url.endswith("._AC_SL1500_.jpg"):
                return None
            if url.endswith("._AC_SX679_.jpg"):
                return TINY_PNG
            raise AssertionError(url)

        good_html.calls = []
        flaky_image.urls = []
        original_html = hd.fetch_amazon_html
        original_image = hd.fetch_amazon_image
        hd.fetch_amazon_html = good_html
        hd.fetch_amazon_image = flaky_image
        try:
            response = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.ca/dp/B0FALLBK01"},
            )
            body = response.get_json()
            self.assertTrue(body["cover_token"])
            self.assertNotIn(hd.MSG_NO_COVER, body["messages"])
            self.assertEqual(good_html.calls, ["https://www.amazon.ca/dp/B0FALLBK01"])
            self.assertEqual(
                flaky_image.urls,
                [
                    "https://m.media-amazon.com/images/I/71fall._AC_SL1500_.jpg",
                    "https://m.media-amazon.com/images/I/71fall._AC_SX679_.jpg",
                ],
            )
        finally:
            hd.fetch_amazon_html = original_html
            hd.fetch_amazon_image = original_image


if __name__ == "__main__":
    unittest.main()
