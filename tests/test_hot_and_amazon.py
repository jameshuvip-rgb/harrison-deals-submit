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
        self.assertIn("grid-template-columns:repeat(3, minmax(0, 1fr))", html)
        self.assertNotIn("Today's pick", html)
        self.assertEqual(feat_ids(html), ["B0HC3N5BHW", "B0HF7Q25KL", "B0HF81MTRH"])
        self.assertIn("Most viewed in 30 minutes, biggest discount, then most saved", html)
        self.assertIn(">Save<", html)

    def test_search_hides_hot_deals_and_category_stays_in_pool(self):
        search = self._html("/?q=tesla")
        self.assertEqual(search.count('class="feat-card"'), 0)
        self.assertNotIn("Most viewed in 30 minutes", search)
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
        self.assertIn("Most viewed", html)
        self.assertIn("1 view in 30 min", html)

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
        self.assertIn("Saved · 1", html)

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
        self.assertIn("3 saved", html)

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

        def no_image(url):
            raise AssertionError(url)

        original_html = hd.fetch_amazon_html
        original_image = hd.fetch_amazon_image
        hd.fetch_amazon_html = no_html
        hd.fetch_amazon_image = no_image
        try:
            bad = self.client.post("/api/amazon-lookup", json={"url": "https://example.com/dp/B0HHJ93L1P"})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(calls, [])
            soft = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.com/Some-Name/dp/B0HHJ93L1P?ref=abc"},
            )
            payload = soft.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["asin"], "B0HHJ93L1P")
            self.assertIsNone(payload["deal_price"])
            self.assertIn(hd.MSG_NO_PRICE, payload["messages"])
            self.assertIn(hd.MSG_NO_COVER, payload["messages"])
            self.assertEqual(calls, ["https://www.amazon.com/dp/B0HHJ93L1P"])

            def good_html(url, accept_language):
                calls.append((url, accept_language))
                return SAMPLE_HTML

            def good_image(url):
                self.assertTrue(url.startswith("https://m.media-amazon.com/"))
                return TINY_PNG

            hd.fetch_amazon_html = good_html
            hd.fetch_amazon_image = good_image
            filled = self.client.post(
                "/api/amazon-lookup",
                json={"url": "https://www.amazon.ca/dp/B0HHJ93L1P"},
            )
            body = filled.get_json()
            self.assertEqual(body["messages"], [hd.MSG_FILLED])
            self.assertEqual(body["title"], "Honda Cabin Filter Kit")
            self.assertEqual(body["deal_price"], 19.99)
            self.assertEqual(body["list_price"], 34.99)
            self.assertTrue(body["cover_token"])
            self.assertEqual(calls[-1][0], "https://www.amazon.ca/dp/B0HHJ93L1P")
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
                "amazon_url": "https://www.amazon.ca/dp/B0HHJ93L1P",
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
        self.assertEqual(row[1], "B0HHJ93L1P")
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


if __name__ == "__main__":
    unittest.main()
