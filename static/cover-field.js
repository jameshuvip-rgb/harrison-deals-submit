(function () {
  function sniff(buf) {
    var b = new Uint8Array(buf);
    if (b.length >= 4 && b[0] === 0x38 && b[1] === 0x42 && b[2] === 0x50 && b[3] === 0x53) return "psd";
    function at(i) {
      if (i + 3 <= b.length && b[i] === 0xff && b[i + 1] === 0xd8 && b[i + 2] === 0xff) return "jpg";
      if (i === 0 && b.length >= 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return "png";
      if (i === 0 && b.length >= 6 && b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return "gif";
      if (
        i === 0 && b.length >= 12 &&
        b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 &&
        b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50
      ) return "webp";
      return "";
    }
    var direct = at(0);
    if (direct) return direct;
    var end = Math.min(b.length - 2, 64);
    for (var i = 1; i < end; i++) {
      if (at(i) === "jpg") return "jpg";
    }
    return "";
  }

  document.querySelectorAll("[data-cover-field]").forEach(function (root) {
    var input = root.querySelector("[data-cover-input]");
    var clearBtn = root.querySelector("[data-cover-clear]");
    var picked = root.querySelector("[data-cover-picked]");
    var neu = root.querySelector("[data-cover-new]");
    var img = root.querySelector("[data-cover-preview]");
    var err = root.querySelector("[data-cover-error]");
    var maxBytes = parseInt(root.getAttribute("data-max-bytes") || "0", 10) || (8 * 1024 * 1024);
    var url = null;

    function setError(msg) {
      if (!msg) {
        err.hidden = true;
        err.textContent = "";
        delete root.dataset.invalid;
        return;
      }
      err.hidden = false;
      err.textContent = msg;
      root.dataset.invalid = "1";
    }

    function resetPreview() {
      if (url) {
        URL.revokeObjectURL(url);
        url = null;
      }
      img.removeAttribute("src");
      neu.hidden = true;
    }

    function clearFile() {
      input.value = "";
      resetPreview();
      picked.hidden = true;
      picked.textContent = "";
      clearBtn.hidden = true;
      setError("");
    }

    clearBtn.addEventListener("click", clearFile);

    input.addEventListener("change", function () {
      resetPreview();
      setError("");
      var file = input.files && input.files[0];
      if (!file) {
        picked.hidden = true;
        picked.textContent = "";
        clearBtn.hidden = true;
        return;
      }
      clearBtn.hidden = false;
      picked.hidden = false;
      picked.textContent = "Selected: " + file.name;
      if (!file.size) {
        setError("That file is empty. Choose a JPG, PNG, WEBP, or GIF.");
        return;
      }
      if (file.size > maxBytes) {
        var mb = (file.size / (1024 * 1024)).toFixed(1);
        setError("This image is " + mb + "MB, over the 8MB limit. Choose a smaller JPG, PNG, WEBP, or GIF.");
        return;
      }
      var reader = new FileReader();
      reader.onload = function () {
        var kind = sniff(reader.result);
        if (kind === "psd") {
          setError("This is a Photoshop PSD file. Export the artboard as a JPG or PNG under 8MB and upload that file.");
          return;
        }
        if (!kind) {
          setError("This file isn't a JPG, PNG, WEBP, or GIF. Export it as a standard JPG or PNG under 8MB and try again.");
          return;
        }
        url = URL.createObjectURL(file);
        img.onload = function () { neu.hidden = false; };
        img.onerror = function () {
          neu.hidden = true;
          err.hidden = false;
          err.textContent = "Preview isn't available for this file. You can still save it if it is a JPG or PNG under 8MB.";
          delete root.dataset.invalid;
        };
        img.src = url;
      };
      reader.onerror = function () {
        setError("Couldn't read that file. Try a JPG or PNG under 8MB.");
      };
      reader.readAsArrayBuffer(file.slice(0, 64));
    });

    var form = root.closest("form");
    if (form) {
      form.addEventListener("submit", function (ev) {
        if (root.dataset.invalid === "1") {
          ev.preventDefault();
          err.hidden = false;
          err.focus && err.setAttribute("tabindex", "-1");
          err.scrollIntoView({ block: "nearest" });
        }
      });
    }
  });
})();
