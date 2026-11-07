(function () {
  var form = document.querySelector("form.card");
  if (!form) return;
  var urlInput = form.querySelector('input[name="amazon_url"]');
  if (!urlInput) return;

  var asinInput = form.querySelector('input[name="asin"]');
  var titleInput = form.querySelector('input[name="title"]');
  var priceInput = form.querySelector('input[name="deal_price"]');
  var listInput = form.querySelector('input[name="list_price"]');
  var statusEl = form.querySelector("[data-amazon-status]");
  var coverRoot = form.querySelector("[data-cover-field]");
  var timer = null;
  var seq = 0;
  var ASIN_IN_PATH = /(?:\/dp\/|\/gp\/product\/|\/gp\/aw\/d\/|\/product\/)([A-Za-z0-9]{10})(?:[/?#]|$)/i;

  function marketplace(raw) {
    var value = String(raw || "").trim();
    if (!value) return null;
    var withScheme = /^https?:\/\//i.test(value) ? value : "https://" + value;
    var parsed;
    try {
      parsed = new URL(withScheme);
    } catch (err) {
      return null;
    }
    var host = parsed.hostname.toLowerCase();
    var market = "";
    if (host === "amazon.ca" || host.endsWith(".amazon.ca")) market = "ca";
    else if (host === "amazon.com" || host.endsWith(".amazon.com")) market = "com";
    if (!market) return null;
    var match = parsed.pathname.match(ASIN_IN_PATH);
    var asin = match ? match[1].toUpperCase() : "";
    if (!asin) {
      var queryAsin = (parsed.searchParams.get("asin") || "").toUpperCase();
      if (/^[A-Z0-9]{10}$/.test(queryAsin)) asin = queryAsin;
    }
    if (!/^[A-Z0-9]{10}$/.test(asin)) return null;
    return { asin: asin, market: market };
  }

  function setStatus(messages, kind) {
    if (!statusEl) return;
    statusEl.replaceChildren();
    if (!messages || !messages.length) {
      statusEl.hidden = true;
      statusEl.className = "amazon-status";
      return;
    }
    messages.forEach(function (message) {
      var line = document.createElement("span");
      line.textContent = message;
      statusEl.appendChild(line);
    });
    statusEl.hidden = false;
    statusEl.className = "amazon-status " + (kind === "err" ? "err" : "ok");
  }

  function clearScrapedCover() {
    if (!coverRoot) return;
    coverRoot.dispatchEvent(new CustomEvent("hd-scraped-cover-clear"));
  }

  function showScrapedCover(src, token) {
    if (!coverRoot || !src) return;
    coverRoot.dispatchEvent(
      new CustomEvent("hd-scraped-cover", { detail: { src: src, token: token || "" } })
    );
  }

  function lookup(url) {
    var mine = ++seq;
    fetch("/api/amazon-lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ url: url }),
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body || {} };
        });
      })
      .then(function (result) {
        if (mine !== seq) return;
        var body = result.body;
        if (asinInput && !asinInput.disabled && body.asin) asinInput.value = body.asin;
        if (!result.ok || body.ok === false) {
          clearScrapedCover();
          setStatus(
            [body.error || "Couldn't fetch price — enter manually", "Cover unavailable — upload your own"].filter(Boolean),
            "err"
          );
          return;
        }
        if (titleInput && body.title) titleInput.value = body.title;
        if (priceInput && body.deal_price != null && body.deal_price !== "") {
          priceInput.value = Number(body.deal_price).toFixed(2);
        }
        if (listInput && body.list_price != null && body.list_price !== "") {
          listInput.value = Number(body.list_price).toFixed(2);
        }
        if (body.cover_preview && body.cover_token) {
          showScrapedCover(body.cover_preview, body.cover_token);
        } else {
          clearScrapedCover();
        }
        var messages = body.messages && body.messages.length
          ? body.messages
          : ["Couldn't fetch price — enter manually", "Cover unavailable — upload your own"];
        setStatus(messages, body.deal_price == null ? "err" : "ok");
      })
      .catch(function () {
        if (mine !== seq) return;
        clearScrapedCover();
        setStatus(
          ["Couldn't fetch price — enter manually", "Cover unavailable — upload your own"],
          "err"
        );
      });
  }

  function onUrl() {
    var url = urlInput.value.trim();
    var parts = marketplace(url);
    if (asinInput && !asinInput.disabled && parts) asinInput.value = parts.asin;
    if (timer) clearTimeout(timer);
    if (!url) {
      seq += 1;
      setStatus([], "");
      return;
    }
    if (!parts) {
      seq += 1;
      if (/amazon\./i.test(url)) {
        setStatus(["Paste an amazon.ca or amazon.com product link."], "err");
      }
      return;
    }
    setStatus(["Looking up that Amazon link…"], "ok");
    timer = setTimeout(function () {
      lookup(url);
    }, 400);
  }

  urlInput.addEventListener("input", onUrl);
  urlInput.addEventListener("change", onUrl);
})();
