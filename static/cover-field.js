(function () {
  function sniff(buf) {
    var b = new Uint8Array(buf);
    if (b.length >= 4 && b[0] === 0x38 && b[1] === 0x42 && b[2] === 0x50 && b[3] === 0x53) return "psd";
    function at(i) {
      if (i + 3 <= b.length && b[0] === 0xff && b[i + 1] === 0xd8 && b[i + 2] === 0xff) return "jpg";
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
    var slot = root.querySelector("[data-cover-slot]");
    var img = root.querySelector("[data-cover-preview]");
    var note = root.querySelector("[data-cover-note]");
    var err = root.querySelector("[data-cover-error]");
    var clearBtn = root.querySelector("[data-clear-cover]");
    var removeInput = root.querySelector("[data-remove-cover]");
    var maxBytes = parseInt(root.getAttribute("data-max-bytes") || "0", 10) || (8 * 1024 * 1024);
    var savedSrc = root.getAttribute("data-saved-src") || "";
    var url = null;
    var generation = 0;
    var silent = false;

    if (!input || !slot || !img) return;
    input.multiple = false;
    input.removeAttribute("multiple");

    function setError(msg) {
      if (!err) return;
      if (!msg) {
        err.hidden = true;
        err.textContent = "";
        return;
      }
      err.hidden = false;
      err.textContent = msg;
    }

    function setBlocking(on) {
      if (on) root.dataset.invalid = "1";
      else delete root.dataset.invalid;
    }

    function setNote(msg) {
      if (!note) return;
      if (!msg) {
        note.hidden = true;
        note.textContent = "";
        return;
      }
      note.hidden = false;
      note.textContent = msg;
    }

    function revoke() {
      if (url) {
        URL.revokeObjectURL(url);
        url = null;
      }
    }

    function removalRequested() {
      return !!(removeInput && removeInput.value === "1");
    }

    function showSaved() {
      revoke();
      img.onload = null;
      img.onerror = null;
      if (savedSrc && !removalRequested()) {
        if (img.getAttribute("src") !== savedSrc) img.src = savedSrc;
        img.alt = "Cover preview";
        slot.hidden = false;
        setNote("");
        return;
      }
      img.removeAttribute("src");
      slot.hidden = true;
      if (savedSrc && removalRequested()) {
        setNote("Current cover will be removed when you save. Upload a new photo if you want a replacement.");
      } else {
        setNote("");
      }
    }

    function clearInput() {
      silent = true;
      input.value = "";
      silent = false;
    }

    function rejectFile(msg) {
      clearInput();
      setBlocking(false);
      setError(msg);
      showSaved();
    }

    function showReplacement(file) {
      revoke();
      if (removeInput) removeInput.value = "";
      url = URL.createObjectURL(file);
      slot.hidden = false;
      img.alt = "Cover preview";
      img.onload = function () { slot.hidden = false; };
      img.onerror = function () {
        slot.hidden = false;
        if (err) {
          err.hidden = false;
          err.textContent = "Preview isn't available for this file. You can still save it if it is a JPG or PNG under 8MB.";
        }
        delete root.dataset.invalid;
      };
      img.src = url;
      var picked = "Selected: " + file.name;
      setNote(savedSrc ? picked + ". This replaces the current cover when you save." : picked);
    }

    // A later pick replaces the earlier one. Never keep two files on the input.
    function soleFile() {
      var list = input.files;
      if (!list || !list.length) return null;
      var file = list[list.length - 1];
      if (list.length === 1) return file;
      try {
        var dt = new DataTransfer();
        dt.items.add(file);
        silent = true;
        input.files = dt.files;
        silent = false;
        return (input.files && input.files[0]) || file;
      } catch (e) {
        silent = false;
        return file;
      }
    }

    // One slot. Clear drops whatever is showing; a saved cover is removed on save.
    function clearCover() {
      generation += 1;
      clearInput();
      setBlocking(false);
      setError("");
      if (removeInput && savedSrc) removeInput.value = "1";
      showSaved();
    }

    if (clearBtn) clearBtn.addEventListener("click", clearCover);

    input.addEventListener("change", function () {
      if (silent) return;
      var gen = ++generation;
      setError("");
      var file = soleFile();
      if (!file) {
        setBlocking(false);
        showSaved();
        return;
      }
      if (!file.size) {
        rejectFile("That file is empty. Choose a JPG, PNG, WEBP, or GIF.");
        return;
      }
      if (file.size > maxBytes) {
        var mb = (file.size / (1024 * 1024)).toFixed(1);
        rejectFile("This image is " + mb + "MB, over the 8MB limit. Choose a smaller JPG, PNG, WEBP, or GIF.");
        return;
      }
      setBlocking(true);
      var reader = new FileReader();
      reader.onload = function () {
        if (gen !== generation) return;
        var kind = sniff(reader.result);
        if (kind === "psd") {
          rejectFile("This is a Photoshop PSD file. Export the artboard as a JPG or PNG under 8MB and upload that file.");
          return;
        }
        if (!kind) {
          rejectFile("This file isn't a JPG, PNG, WEBP, or GIF. Export it as a standard JPG or PNG under 8MB and try again.");
          return;
        }
        setBlocking(false);
        if (input.files && input.files.length > 1) soleFile();
        showReplacement(file);
      };
      reader.onerror = function () {
        if (gen !== generation) return;
        rejectFile("Couldn't read that file. Try a JPG or PNG under 8MB.");
      };
      reader.readAsArrayBuffer(file.slice(0, 64));
    });

    var form = root.closest("form");
    if (form) {
      form.addEventListener("submit", function (ev) {
        if (input.files && input.files.length > 1) soleFile();
        if (root.dataset.invalid === "1") {
          ev.preventDefault();
          if (err) {
            err.hidden = false;
            err.setAttribute("tabindex", "-1");
            err.scrollIntoView({ block: "nearest" });
          }
        }
      });
    }
  });
})();
