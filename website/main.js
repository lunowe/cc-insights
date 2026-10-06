(function () {
  "use strict";
  document.documentElement.classList.add("js");

  /* ── copy to clipboard ─────────────────────────────────────────────── */
  var toast = document.getElementById("toast");
  var toastTimer = null;

  function showToast(text) {
    if (!toast) return;
    toast.textContent = text;
    toast.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.remove("is-on"); }, 1600);
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () {
        return fallbackCopy(text);
      });
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function markCopied(button, ok) {
    var label = button.querySelector(".copy-label");
    var original = button.getAttribute("data-label") || (label ? label.textContent : "Copy");
    button.setAttribute("data-label", original);
    if (label) label.textContent = ok ? "Copied" : "Press ⌘C";
    button.classList.toggle("is-copied", ok);
    showToast(ok ? "Copied to clipboard" : "Could not copy. Select the text and copy it yourself.");
    clearTimeout(button._t);
    button._t = setTimeout(function () {
      if (label) label.textContent = original;
      button.classList.remove("is-copied");
    }, 1800);
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (button) {
    button.addEventListener("click", function () {
      var target = document.querySelector(button.getAttribute("data-copy"));
      if (!target) return;
      var text = target.textContent.replace(/\s+$/, "");
      copyText(text).then(function (ok) {
        if (!ok) {
          // Make manual copying easy: select the source text.
          var range = document.createRange();
          range.selectNodeContents(target);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
        markCopied(button, ok);
      });
    });
  });

  /* ── expand / collapse the agent prompt ───────────────────────────── */
  var wrap = document.getElementById("prompt-wrap");
  var toggle = document.getElementById("prompt-toggle");
  if (wrap && toggle) {
    toggle.addEventListener("click", function () {
      var collapsed = wrap.classList.toggle("is-collapsed");
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.textContent = collapsed ? "Show the whole prompt" : "Collapse the prompt";
    });
  }

  /* ── scroll-in for the charts ──────────────────────────────────────── */
  var charts = document.querySelectorAll(".chart");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in-view");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.2 });
    Array.prototype.forEach.call(charts, function (c) { io.observe(c); });
  } else {
    Array.prototype.forEach.call(charts, function (c) { c.classList.add("in-view"); });
  }
})();
