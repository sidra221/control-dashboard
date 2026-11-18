/* PEPLO landing — حركات خفيفة (تحسين تدريجي: الصفحة كاملة بدون JS، وبدون حركة مع prefers-reduced-motion). */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = root.classList.contains("rm");
  var $ = function (s, c) { return (c || document).querySelector(s); };
  var $$ = function (s, c) { return Array.prototype.slice.call((c || document).querySelectorAll(s)); };
  var supportsIO = "IntersectionObserver" in window;

  function once(els, cb, opts) {
    if (!supportsIO) { els.forEach(function (e) { cb(e); }); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { io.unobserve(en.target); cb(en.target); }
      });
    }, opts || { threshold: 0.18, rootMargin: "0px 0px -6% 0px" });
    els.forEach(function (e) { io.observe(e); });
  }

  /* ---------- شريط التقدّم + حالة الـ nav ---------- */
  var nav = $(".nav");
  var bar = $(".progress");
  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var y = window.scrollY || 0;
      var max = root.scrollHeight - window.innerHeight;
      if (nav) nav.classList.toggle("is-scrolled", y > 8);
      if (bar && max > 0) bar.style.transform = "scaleX(" + Math.min(1, y / max).toFixed(4) + ")";
      ticking = false;
    });
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---------- الرابط النشط بالـ nav ---------- */
  if (supportsIO) {
    var links = {};
    $$(".nav-links a").forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        var a = links[en.target.id];
        if (a && en.isIntersecting) {
          Object.keys(links).forEach(function (k) { links[k].classList.remove("is-active"); });
          a.classList.add("is-active");
        }
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    Object.keys(links).forEach(function (id) { var s = document.getElementById(id); if (s) spy.observe(s); });
  }

  /* ---------- مقارنة قبل/بعد: تشتغل دائماً، والجولة التلقائية بس بدون reduced-motion ---------- */
  $$("[data-ba]").forEach(function (ba) {
    var range = $(".ba-range", ba);
    var touched = false;
    function set(v) { ba.style.setProperty("--p", v + "%"); }
    range.addEventListener("input", function () { touched = true; set(range.value); });
    ba.addEventListener("pointerdown", function () { touched = true; });
    if (reduce || !supportsIO) return;
    once([ba], function () {
      var t0 = null, dur = 3800;
      function frame(ts) {
        if (touched) return;
        if (t0 === null) t0 = ts;
        var p = Math.min(1, (ts - t0) / dur);
        var v = 50 + 32 * Math.sin(p * Math.PI * 2);
        set(v.toFixed(1)); range.value = v;
        if (p < 1) requestAnimationFrame(frame);
      }
      setTimeout(function () { requestAnimationFrame(frame); }, 700);
    }, { threshold: 0.55 });
  });

  if (reduce) { return; }

  /* ---------- ظهور العناصر عند السكرول (مع تدرّج بالتأخير) ---------- */
  var revealSel = ".section h2, .section .lead, .section .fine, .eyebrow, .card, .steps li, .snippet, .ticks li, " +
                  ".chips li, .kpi, .faq details, .caption, .ba, .ba-product, .cta h2, .cta .hero-actions";
  var groups = new Map();
  $$(revealSel).forEach(function (el) {
    var p = el.parentElement;
    var i = groups.get(p) || 0;
    groups.set(p, i + 1);
    el.style.setProperty("--d", Math.min(i, 6) * 90 + "ms");
  });
  once($$(revealSel), function (el) { el.classList.add("is-in"); });
  once($$(".meander"), function (el) { el.classList.add("is-in"); }, { threshold: 0.3 });

  /* ---------- محادثة: كتابة ثم رد ---------- */
  function playChat(chat) {
    var items = $$(".msg, .pcard", chat);
    var t = 350;
    items.forEach(function (it) {
      var isBot = it.classList.contains("msg--bot");
      if (isBot) {
        var typing = document.createElement("div");
        typing.className = "typing";
        typing.setAttribute("aria-hidden", "true");
        typing.innerHTML = "<i></i><i></i><i></i>";
        (function (tp, ref, at) {
          setTimeout(function () { ref.parentNode.insertBefore(tp, ref); tp.classList.add("is-shown"); }, at);
          setTimeout(function () { tp.remove(); }, at + 1050);
        })(typing, it, t);
        t += 1050;
      }
      (function (el, at) { setTimeout(function () { el.classList.add("is-shown"); }, at); })(it, t);
      t += it.classList.contains("pcard") ? 450 : 700;
    });
  }
  var heroChat = $(".hero .chat");
  if (heroChat) setTimeout(function () { playChat(heroChat); }, 900);
  once($$(".chats .chat"), playChat, { threshold: 0.5 });

  /* ---------- عدّاد الأرقام ---------- */
  var nf = new Intl.NumberFormat("ar-EG");
  once($$(".kpi b"), function (el) {
    var target = parseInt(el.getAttribute("data-count"), 10);
    if (isNaN(target)) return;
    var start = null, dur = 1400;
    function step(ts) {
      if (start === null) start = ts;
      var p = Math.min(1, (ts - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = nf.format(Math.round(target * eased));
      if (p < 1) requestAnimationFrame(step);
    }
    el.textContent = nf.format(0);
    requestAnimationFrame(step);
  }, { threshold: 0.6 });

  /* ---------- ميلان بسيط للمحادثة العائمة مع حركة الماوس ---------- */
  var visual = $(".hero-visual");
  var floatChat = $(".chat--float");
  if (visual && floatChat && window.matchMedia("(pointer: fine)").matches) {
    visual.addEventListener("mousemove", function (e) {
      var r = visual.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width - 0.5;
      var y = (e.clientY - r.top) / r.height - 0.5;
      floatChat.style.transform = "rotate(-1.2deg) translate(" + (x * -10).toFixed(1) + "px," + (y * -8).toFixed(1) + "px)";
    });
    visual.addEventListener("mouseleave", function () { floatChat.style.transform = ""; });
  }
})();
