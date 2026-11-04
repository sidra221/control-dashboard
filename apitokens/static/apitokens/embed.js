(function () {
  "use strict";

  var scriptEl = document.currentScript;
  var STORE_KEY = scriptEl ? scriptEl.getAttribute("data-store-key") : null;
  var POSITION = (scriptEl && scriptEl.getAttribute("data-position")) || "right";
  var SIDE = POSITION === "left" ? "left" : "right";
  var API_BASE = (function () {
    try {
      return new URL(scriptEl.src).origin;
    } catch (e) {
      return "";
    }
  })();

  if (!STORE_KEY || !API_BASE) {
    console.error("[shopping-widget] missing data-store-key or script src origin");
    return;
  }

  var history = [];
  var hasEngaged = false;
  // معرّف المحادثة: بيربط رسائلها وأحداثها (وطلب Shopify لو صار) ببعض بالسيرفر.
  var SESSION_ID = (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random())
    .replace(/[^A-Za-z0-9_-]/g, "")
    .slice(0, 64);
  var shownSkus = [];
  // معرّف ثابت للزبون (بيضل بعد تحديث الصفحة) — منه بيتحسب حد تجارب اللبس.
  var SHOPPER_ID = (function () {
    var key = "cw_shopper_" + STORE_KEY.slice(0, 8);
    try {
      var saved = window.localStorage.getItem(key);
      if (saved) return saved;
      window.localStorage.setItem(key, SESSION_ID);
    } catch (e) {}
    return SESSION_ID;
  })();
  // جوا متجر Shopify فعلي: منقدر نضيف للسلة مباشرة ونعلّم السلة بمعرّف المحادثة.
  var IS_SHOPIFY = !!(window.Shopify && window.Shopify.shop);
  // جوا متجر سلة: مكتبة سلة (Twilight) بتضيف للسلة، ورقم السلة بيربط الطلب بالمحادثة.
  var IS_SALLA = !!(window.salla && window.salla.cart && window.salla.cart.addItem);
  // زد: ثيمات Vitrin الجديدة (zid.cart) والقديمة (zid.store.cart).
  var ZID_CART = window.zid && (window.zid.cart || (window.zid.store && window.zid.store.cart));
  var IS_ZID = !!(ZID_CART && ZID_CART.addProduct);
  // متجر مخصص (متل متجر الديمو) بيقدر يعرّف window.ControlCart = {add, url} لنضيف لسلته.
  function controlCart() {
    return window.ControlCart && typeof window.ControlCart.add === "function" ? window.ControlCart : null;
  }
  // المقاس اللي رشّحه المساعد لكل منتج — بيطلع كشارة على الكرت وبينضاف للسلة معه.
  var sizeBySku = {};
  var recBySku = {};
  var cartTagged = false;
  var UI = {
    ar: { add: "أضف للسلة 🛒", added: "انضاف للسلة ✓ — كمّل طلبك", addFailed: "ما قدرنا نضيفه للسلة، جرّب من صفحة المنتج.", buy: "اشترِه الحين" },
    en: { add: "Add to cart 🛒", added: "Added to cart ✓ — checkout", addFailed: "Couldn't add it to the cart, try from the product page.", buy: "Buy now" },
  };
  function uiLang() {
    var lastUser = "";
    for (var i = history.length - 1; i >= 0; i--) {
      if (history[i].role === "user") { lastUser = history[i].content; break; }
    }
    return /[\u0600-\u06FF]/.test(lastUser) || !lastUser ? "ar" : "en";
  }

  // لما تخلص تجارب الزبون: كل أزرار التجربة بتصير رمادية ومعطّلة (والجديدة كمان).
  var tryonsExhausted = false;
  function disableTryons() {
    tryonsExhausted = true;
    if (typeof msgsEl === "undefined" || !msgsEl) return;
    var buttons = msgsEl.querySelectorAll("button.tryon");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = true;
      buttons[i].textContent = "خلصت تجاربك 🙏";
    }
  }

  // لو الزبون فاتح صفحة منتج من الكتالوج، منفتح المحادثة على هالمنتج نفسه.
  var pageProduct = null;
  var pageProductReady = fetch(API_BASE + "/widget/page-product/", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Api-Token": STORE_KEY },
    body: JSON.stringify({ page_url: window.location.href, shopper_id: SHOPPER_ID }),
  })
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data && data.ok && data.product) pageProduct = data.product;
      if (data && data.tryons_left === 0) disableTryons();
    })
    .catch(function () {});

  function shortTitle(title) {
    return title.length > 40 ? title.slice(0, 38) + "…" : title;
  }

  function sendEvent(type, cartId) {
    fetch(API_BASE + "/widget/event/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Token": STORE_KEY },
      body: JSON.stringify({ type: type, session_id: SESSION_ID, cart_id: cartId ? String(cartId) : "" }),
      keepalive: true,
    }).catch(function () {});
  }

  // رقم سلة الزبون بالمنصة — منه منعرف إذا الطلب إجا من هالمحادثة.
  function platformCartId() {
    if (IS_SALLA && window.salla.cart.getCurrentCartId) {
      return window.salla.cart.getCurrentCartId();
    }
    if (IS_ZID) {
      var fetchCart = window.zid.cart && window.zid.cart.get
        ? window.zid.cart.get()
        : window.zid.store.cart.fetch();
      return Promise.resolve(fetchCart).then(function (res) {
        var cart = (res && (res.cart || (res.data && res.data.cart))) || res || {};
        return cart.id || cart.session_id || "";
      });
    }
    return null;
  }

  function trackEvent(type) {
    // ممكن ترجّع قيمة أو Promise حسب نسخة مكتبة المنصة.
    try {
      var pending = platformCartId();
      if (pending === null) {
        sendEvent(type);
        return;
      }
      Promise.resolve(pending)
        .then(function (cartId) {
          sendEvent(type, cartId);
        })
        .catch(function () {
          sendEvent(type);
        });
    } catch (e) {
      sendEvent(type);
    }
  }

  function tagShopifyCart() {
    if (!IS_SHOPIFY || cartTagged) return;
    cartTagged = true;
    var attrs = {};
    attrs["_sw_conversation"] = SESSION_ID;
    fetch("/cart/update.js", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ attributes: attrs }),
    }).catch(function () {
      cartTagged = false;
    });
  }

  var host = document.createElement("div");
  host.id = "sw-widget-host";
  host.style.all = "initial";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent = [
    ":host, * { box-sizing: border-box; }",
    ".bubble {",
    "  position: fixed; bottom: 20px; " + SIDE + ": 20px; z-index: 999999;",
    "  width: 56px; height: 56px; border-radius: 50%;",
    "  background: linear-gradient(135deg, #6366f1, #4f46e5);",
    "  color: #fff; border: none; cursor: pointer;",
    "  box-shadow: 0 8px 24px -8px rgba(99,102,241,.6);",
    "  font-size: 24px; display: flex; align-items: center; justify-content: center;",
    "  font-family: system-ui, sans-serif;",
    "  transition: transform .15s ease;",
    "}",
    ".bubble:hover { transform: scale(1.06); }",
    ".nudge {",
    "  position: fixed; bottom: 86px; " + SIDE + ": 14px; z-index: 999998;",
    "  max-width: 220px; background: #1a1d29; color: #fff; font-size: 12.5px; line-height: 1.5;",
    "  padding: 10px 14px; border-radius: 12px; box-shadow: 0 8px 24px -6px rgba(0,0,0,.35);",
    "  font-family: 'Tajawal', system-ui, -apple-system, 'Segoe UI', Tahoma, Arial, sans-serif;",
    "  display: none; cursor: pointer; direction: rtl; text-align: right;",
    "}",
    ".nudge.open { display: block; animation: sw-nudge-in .25s ease; }",
    "@keyframes sw-nudge-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }",
    ".panel {",
    "  position: fixed; bottom: 86px; " + SIDE + ": 20px; z-index: 999999;",
    "  width: 340px; max-width: calc(100vw - 40px); height: 480px; max-height: calc(100vh - 120px);",
    "  background: #fff; border-radius: 16px; box-shadow: 0 12px 40px -8px rgba(0,0,0,.25);",
    "  display: none; flex-direction: column; overflow: hidden;",
    "  font-family: 'Tajawal', system-ui, -apple-system, 'Segoe UI', Tahoma, Arial, sans-serif;",
    "  border: 1px solid #e4e6ef;",
    "}",
    ".panel.open { display: flex; }",
    ".head {",
    "  background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff;",
    "  padding: 14px 16px; font-weight: 700; font-size: 14px;",
    "  display: flex; align-items: center; justify-content: space-between;",
    "}",
    ".head button {",
    "  background: rgba(255,255,255,.2); border: none; color: #fff; border-radius: 8px;",
    "  width: 26px; height: 26px; cursor: pointer; font-size: 14px; line-height: 1;",
    "}",
    ".msgs { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; background: #f8f9fc; }",
    ".msg { max-width: 82%; padding: 9px 13px; border-radius: 14px; font-size: 13.5px; line-height: 1.45; white-space: pre-wrap; }",
    ".msg.user { align-self: flex-end; background: #6366f1; color: #fff; border-bottom-left-radius: 4px; }",
    ".msg.bot { align-self: flex-start; background: #eef0ff; color: #1a1d29; border-bottom-right-radius: 4px; }",
    ".msg.error { align-self: center; background: #fef2f2; color: #b91c1c; font-size: 12.5px; }",
    ".msg.discount {",
    "  align-self: center; background: #fff7ed; color: #9a3412; border: 1px dashed #fb923c;",
    "  font-size: 13px; font-weight: 700; text-align: center;",
    "}",
    ".card { align-self: flex-start; max-width: 88%; background: #fff; border: 1px solid #e4e6ef; border-radius: 14px; padding: 10px; }",
    ".card img { width: 100%; border-radius: 10px; display: block; margin-bottom: 8px; max-height: 180px; object-fit: cover; }",
    ".card .title { font-size: 13px; font-weight: 700; margin-bottom: 2px; }",
    ".card .price { font-size: 12.5px; color: #6b7083; margin-bottom: 8px; }",
    ".card .price s { opacity: .7; margin-inline-start: 6px; }",
    ".card .price .sale { color: #dc2626; font-weight: 700; }",
    ".card .size-badge { display: inline-block; margin-bottom: 8px; padding: 3px 10px; border-radius: 999px; background: #e8f7ee; color: #12753f; font-size: 12px; font-weight: 700; }",
    ".card button, .card a.buy {",
    "  display: inline-block; text-decoration: none; text-align: center;",
    "  font-size: 12.5px; font-weight: 700; border-radius: 9px; padding: 8px 10px;",
    "  border: none; cursor: pointer; width: 100%; margin-top: 6px; font-family: inherit;",
    "}",
    ".card button.tryon { background: #eef0ff; color: #4f46e5; }",
    ".card button.tryon:disabled { background: #eceef2; color: #9aa0ab; cursor: not-allowed; }",
    ".card a.buy, .card button.addcart { background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff; }",
    ".typing { align-self: flex-start; font-size: 12px; color: #9095ab; padding: 0 4px; }",
    ".msg.writing::after { content: '▍'; margin-inline-start: 1px; opacity: .6; animation: sw-blink 1s steps(1) infinite; }",
    "@keyframes sw-blink { 50% { opacity: 0; } }",
    ".inputrow { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #e4e6ef; background: #fff; }",
    ".inputrow input[type=text] {",
    "  flex: 1; border: 1px solid #e4e6ef; border-radius: 10px; padding: 9px 12px; font-size: 13px;",
    "  font-family: inherit; outline: none;",
    "}",
    ".inputrow input[type=text]:focus { border-color: #6366f1; }",
    ".inputrow button {",
    "  background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff; border: none;",
    "  border-radius: 10px; padding: 0 14px; cursor: pointer; font-weight: 700; font-size: 13px;",
    "}",
    ".inputrow button:disabled { opacity: .5; cursor: default; }",
    ".card.result img { cursor: zoom-in; }",
    ".lightbox {",
    "  position: fixed; inset: 0; z-index: 9999999; background: rgba(0,0,0,.85);",
    "  display: none; align-items: center; justify-content: center; padding: 24px;",
    "}",
    ".lightbox.open { display: flex; }",
    ".lightbox img { max-width: 100%; max-height: 100%; border-radius: 10px; box-shadow: 0 20px 60px -10px rgba(0,0,0,.5); }",
    ".lightbox .lb-close {",
    "  position: absolute; top: 16px; " + SIDE + ": 16px;",
    "  width: 36px; height: 36px; border-radius: 50%; border: none; cursor: pointer;",
    "  background: rgba(255,255,255,.15); color: #fff; font-size: 16px; line-height: 1;",
    "}",
  ].join("\n");
  root.appendChild(style);

  var bubble = document.createElement("button");
  bubble.className = "bubble";
  bubble.innerHTML =
    '<svg width="26" height="26" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M12 3C7.03 3 3 6.58 3 11c0 2.49 1.28 4.7 3.3 6.17-.11.93-.45 2.22-1.27 3.49a.5.5 0 00.56.76c1.84-.56 3.26-1.42 4.1-2.03.74.14 1.52.21 2.31.21 4.97 0 9-3.58 9-8s-4.03-8-9-8z" fill="#fff"/>' +
    '<path d="M12.3 7.6l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z" fill="#4f46e5"/>' +
    "</svg>";
  bubble.title = "تحتاج مساعدة في اختيار إطلالتك؟";
  root.appendChild(bubble);

  var NUDGE_MESSAGES = [
    "تقدر تشوف شكل أي قطعة عليك الحين قبل لا تطلبها — جرّب القياس الافتراضي! 😍",
    "ليش تطلب وأنت مو متأكد؟ جرّب القطعة عليك افتراضياً أول وشوف شكلها! ✨",
    "خلّك ذكي في تسوّقك — جرّب الملابس عليك قبل لا تشتري! 🛍️",
  ];
  var nudgeIndex = 0;
  var nudgeHideTimer = null;
  var nudge = document.createElement("div");
  nudge.className = "nudge";
  root.appendChild(nudge);

  var panel = document.createElement("div");
  panel.className = "panel";
  panel.innerHTML =
    '<div class="head"><span>مساعد التسوق</span><button class="close">✕</button></div>' +
    '<div class="msgs"></div>' +
    '<div class="inputrow">' +
    '<input type="text" placeholder="اكتب رسالتك..." />' +
    "<button class=\"send\">إرسال</button>" +
    "</div>";
  root.appendChild(panel);

  var msgsEl = panel.querySelector(".msgs");
  var inputEl = panel.querySelector('input[type="text"]');
  var sendBtn = panel.querySelector(".send");
  var closeBtn = panel.querySelector(".close");

  var fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.style.display = "none";
  root.appendChild(fileInput);
  var pendingSku = null;
  var savedPhoto = null;

  var lightbox = document.createElement("div");
  lightbox.className = "lightbox";
  lightbox.innerHTML = '<button class="lb-close">✕</button><img>';
  root.appendChild(lightbox);
  var lightboxImg = lightbox.querySelector("img");
  lightbox.addEventListener("click", function () {
    lightbox.classList.remove("open");
  });

  function openPanel() {
    var isFirstOpen = !hasEngaged;
    hasEngaged = true;
    if (nudgeHideTimer) {
      clearTimeout(nudgeHideTimer);
    }
    nudge.classList.remove("open");
    panel.classList.add("open");
    if (isFirstOpen) {
      // منستنى جواب صفحة المنتج شوي بس، حتى ما يتأخر الترحيب لو السيرفر بطيء.
      var welcomed = false;
      var welcome = function () {
        if (welcomed) return;
        welcomed = true;
        if (!pageProduct) {
          addBotMessage(
            "هلا والله! 👋 وصلت للمكان الصح — قل لي وش في بالك، وأدلّك على أحلى خيار. وإذا تبي، " +
              "تقدر بعد تشوف شكل القطعة عليك قبل لا تقرر 😊"
          );
          return;
        }
        var name = "«" + shortTitle(pageProduct.title) + "»";
        var text = pageProduct.tryon_supported === false
          ? "هلا والله! 👋 شكلك مهتم بـ" + name + ". تبي تعرف عنه أكثر، أو أطلع لك خيارات قريبة منه؟ أنا حاضر 👌"
          : "هلا والله! 👋 شكل " + name + " عاجبك 😍 ودّك تشوف شكله عليك قبل لا تطلبه؟ ارفع صورتك وجرّبه، " +
            "وإذا ما عجبك أطلع لك خيارات ثانية تناسبك 👌";
        addBotMessage(text, function () {
          addRecommendationCard(pageProduct);
        });
      };
      pageProductReady.then(welcome);
      setTimeout(welcome, 1500);
    }
  }

  bubble.addEventListener("click", function () {
    if (panel.classList.contains("open")) {
      panel.classList.remove("open");
    } else {
      openPanel();
    }
  });
  nudge.addEventListener("click", openPanel);
  closeBtn.addEventListener("click", function () {
    panel.classList.remove("open");
  });

  // أول تنبيه بعد ثواني من فتح الصفحة، وبعدها كل 15 ثانية لحد ما يفتح الزبون المحادثة.
  function showNudge() {
    if (hasEngaged || panel.classList.contains("open")) {
      return;
    }
    var messages = NUDGE_MESSAGES;
    if (pageProduct && pageProduct.tryon_supported !== false) {
      var name = "«" + shortTitle(pageProduct.title) + "»";
      messages = [
        "ودّك تشوف " + name + " عليك قبل لا تطلبه؟ جرّبه افتراضياً الحين! ✨",
        "متردد في " + name + "؟ ارفع صورتك وشوف شكله عليك 😍",
      ];
    }
    nudge.textContent = messages[nudgeIndex % messages.length];
    nudgeIndex++;
    nudge.classList.add("open");
    nudgeHideTimer = setTimeout(function () {
      nudge.classList.remove("open");
    }, 6000);
  }
  setTimeout(showNudge, 4000);
  setInterval(showNudge, 15000);

  function scrollToBottom() {
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  function addUserMessage(text) {
    var el = document.createElement("div");
    el.className = "msg user";
    el.textContent = text;
    msgsEl.appendChild(el);
    scrollToBottom();
  }

  // بيكتب رد البوت حرف حرف بدل ما يطلع دفعة وحدة. Array.from حتى ما
  // ينكسر الإيموجي لنصّين، وسرعة الكتابة بتتأقلم مع طول الرد حتى الرد الطويل
  // ما ياخد أكتر من ~3 ثواني. onDone بيتنادى بس يخلص (لعرض الكروت بعده).
  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function addBotMessage(text, onDone) {
    var el = document.createElement("div");
    el.className = "msg bot";
    msgsEl.appendChild(el);
    var chars = Array.from(text);
    if (reduceMotion || chars.length === 0) {
      el.textContent = text;
      scrollToBottom();
      if (onDone) onDone();
      return;
    }
    el.classList.add("writing");
    var delay = Math.max(8, Math.min(30, 3000 / chars.length));
    var i = 0;
    (function step() {
      i++;
      el.textContent = chars.slice(0, i).join("");
      scrollToBottom();
      if (i < chars.length) {
        setTimeout(step, delay);
      } else {
        el.classList.remove("writing");
        if (onDone) onDone();
      }
    })();
  }

  function addErrorMessage(text) {
    var el = document.createElement("div");
    el.className = "msg error";
    el.textContent = text;
    msgsEl.appendChild(el);
    scrollToBottom();
  }

  function addTyping() {
    var el = document.createElement("div");
    el.className = "typing";
    el.textContent = "يكتب...";
    msgsEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function addDiscountMessage(offer) {
    var el = document.createElement("div");
    el.className = "msg discount";
    var text = "🎁 " + offer.code;
    if (offer.description) {
      text += " — " + offer.description;
    }
    el.textContent = text;
    msgsEl.appendChild(el);
    scrollToBottom();
  }

  function sizeBadge(size) {
    var badge = document.createElement("div");
    badge.className = "size-badge";
    badge.textContent = (uiLang() === "ar" ? "مقاسك: " : "Your size: ") + size + " ✓";
    return badge;
  }

  function controlCartButton(rec, labels) {
    var btn = document.createElement("button");
    btn.className = "addcart";
    btn.textContent = labels.add;
    btn.addEventListener("click", function () {
      btn.disabled = true;
      Promise.resolve(
        controlCart().add({ product_id: rec.product_id, quantity: 1, size: sizeBySku[rec.sku] || "" })
      )
        .then(function () {
          trackEvent("add_to_cart");
          var go = document.createElement("a");
          go.className = "buy";
          go.href = controlCart().url || "#";
          go.textContent = labels.added;
          btn.replaceWith(go);
        })
        .catch(function () {
          btn.disabled = false;
          addErrorMessage(labels.addFailed);
        });
    });
    return btn;
  }

  // المساعد رشّح مقاس: منحط الشارة على كروت هالمنتج، وإذا المتجر بيدعم
  // ControlCart منبدّل رابط "اشتري" بزر إضافة للسلة بالمقاس.
  function markSize(sku, size) {
    sizeBySku[sku] = size;
    var cards = msgsEl.querySelectorAll('.card[data-sku="' + sku.replace(/"/g, "") + '"]');
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var old = card.querySelector(".size-badge");
      if (old) old.remove();
      card.querySelector(".price").after(sizeBadge(size));
      var rec = recBySku[sku];
      var link = card.querySelector("a.buy");
      if (controlCart() && rec && rec.product_id && link && !card.querySelector("button.addcart")) {
        link.replaceWith(controlCartButton(rec, UI[uiLang()]));
      }
    }
  }

  function addRecommendationCard(rec) {
    var card = document.createElement("div");
    card.className = "card";
    card.setAttribute("data-sku", rec.sku);
    recBySku[rec.sku] = rec;
    var img = document.createElement("img");
    img.src = rec.image_url;
    img.alt = rec.title;
    card.appendChild(img);
    var title = document.createElement("div");
    title.className = "title";
    title.textContent = rec.title;
    card.appendChild(title);
    var price = document.createElement("div");
    price.className = "price";
    var priceText = rec.price + (rec.currency ? " " + rec.currency : "");
    if (rec.compare_at_price) {
      var now = document.createElement("span");
      now.className = "sale";
      now.textContent = priceText;
      var was = document.createElement("s");
      was.textContent = rec.compare_at_price;
      price.appendChild(now);
      price.appendChild(was);
    } else {
      price.textContent = priceText;
    }
    if (shownSkus.indexOf(rec.sku) === -1) shownSkus.push(rec.sku);
    card.appendChild(price);
    if (sizeBySku[rec.sku]) card.appendChild(sizeBadge(sizeBySku[rec.sku]));

    var tryBtn = document.createElement("button");
    tryBtn.className = "tryon";
    if (rec.tryon_supported === false) tryBtn.style.display = "none";
    tryBtn.textContent = rec.tryon_button_label || "جرّب شكلك فيه 📸";
    if (tryonsExhausted) {
      tryBtn.disabled = true;
      tryBtn.textContent = "خلصت تجاربك 🙏";
    }
    tryBtn.addEventListener("click", function () {
      if (savedPhoto) {
        runTryon(rec.sku, savedPhoto);
      } else {
        addBotMessage("نصيحة: صورة نص الجسم أو كاملة تعطيك نتيجة أدق من صورة الوجه المقرّبة 📸");
        pendingSku = rec.sku;
        fileInput.value = "";
        fileInput.click();
      }
    });
    card.appendChild(tryBtn);

    var labels = UI[uiLang()];
    if (IS_SHOPIFY && rec.variant_id) {
      var addBtn = document.createElement("button");
      addBtn.className = "addcart";
      addBtn.textContent = labels.add;
      addBtn.addEventListener("click", function () {
        addBtn.disabled = true;
        tagShopifyCart();
        fetch("/cart/add.js", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: [{ id: Number(rec.variant_id), quantity: 1 }] }),
        })
          .then(function (r) {
            if (!r.ok) throw new Error("add_failed");
            trackEvent("add_to_cart");
            var go = document.createElement("a");
            go.className = "buy";
            go.href = "/cart";
            go.textContent = labels.added;
            addBtn.replaceWith(go);
          })
          .catch(function () {
            addBtn.disabled = false;
            addErrorMessage(labels.addFailed);
          });
      });
      card.appendChild(addBtn);
    } else if (controlCart() && rec.product_id && (!rec.has_options || sizeBySku[rec.sku])) {
      card.appendChild(controlCartButton(rec, labels));
    } else if (IS_SALLA && rec.product_id && !rec.has_options) {
      // منتج بمقاسات/ألوان لازم الزبون يختارها بصفحة المنتج — بس البسيط منضيفه من هون.
      var sallaAddBtn = document.createElement("button");
      sallaAddBtn.className = "addcart";
      sallaAddBtn.textContent = labels.add;
      sallaAddBtn.addEventListener("click", function () {
        sallaAddBtn.disabled = true;
        Promise.resolve(window.salla.cart.addItem({ id: Number(rec.product_id), quantity: 1 }))
          .then(function () {
            trackEvent("add_to_cart");
            var go = document.createElement("a");
            go.className = "buy";
            go.href = (window.salla.url && window.salla.url.get && window.salla.url.get("cart")) || "/cart";
            go.textContent = labels.added;
            sallaAddBtn.replaceWith(go);
          })
          .catch(function () {
            sallaAddBtn.disabled = false;
            addErrorMessage(labels.addFailed);
          });
      });
      card.appendChild(sallaAddBtn);
    } else if (IS_ZID && rec.product_id && !rec.has_options) {
      var zidAddBtn = document.createElement("button");
      zidAddBtn.className = "addcart";
      zidAddBtn.textContent = labels.add;
      zidAddBtn.addEventListener("click", function () {
        zidAddBtn.disabled = true;
        var adding = window.zid.cart && window.zid.cart.addProduct
          ? window.zid.cart.addProduct({ product_id: rec.product_id, quantity: 1 })
          : window.zid.store.cart.addProduct({ productId: rec.product_id, quantity: 1 });
        Promise.resolve(adding)
          .then(function () {
            trackEvent("add_to_cart");
            var go = document.createElement("a");
            go.className = "buy";
            go.href = "/cart/view";
            go.textContent = labels.added;
            zidAddBtn.replaceWith(go);
          })
          .catch(function () {
            zidAddBtn.disabled = false;
            addErrorMessage(labels.addFailed);
          });
      });
      card.appendChild(zidAddBtn);
    } else if (rec.product_url) {
      var buyLink = document.createElement("a");
      buyLink.className = "buy";
      buyLink.href = rec.product_url;
      buyLink.target = "_blank";
      buyLink.rel = "noopener";
      buyLink.textContent = labels.buy;
      buyLink.addEventListener("click", function () {
        tagShopifyCart();
        trackEvent("buy_click");
      });
      card.appendChild(buyLink);
    }

    msgsEl.appendChild(card);
    scrollToBottom();
  }

  function addTryonResultCard(imageUrl, productUrl) {
    var card = document.createElement("div");
    card.className = "card result";
    var img = document.createElement("img");
    img.src = imageUrl;
    img.alt = "شكلك فيه — اضغط للتكبير";
    img.title = "اضغط للتكبير";
    img.addEventListener("click", function () {
      lightboxImg.src = imageUrl;
      lightbox.classList.add("open");
    });
    card.appendChild(img);
    if (productUrl) {
      var buyLink = document.createElement("a");
      buyLink.className = "buy";
      buyLink.href = productUrl;
      buyLink.target = "_blank";
      buyLink.rel = "noopener";
      buyLink.textContent = "عجبني، أبي أشتريه";
      card.appendChild(buyLink);
    }
    msgsEl.appendChild(card);
    scrollToBottom();
  }

  var ERROR_MESSAGES = {
    invalid_token: "فيه مشكلة في إعداد المتجر — تواصل مع صاحب المتجر.",
    token_disabled: "الخدمة موقوفة مؤقتاً لهذا المتجر.",
    quota_exceeded: "خلص رصيد الصور المسموح لهذي الفترة.",
    assistant_not_configured: "المساعد الذكي مو جاهز للحين.",
    assistant_unavailable: "فيه ضغط على المساعد الذكي الحين، جرّب بعد شوي 🙏",
    tryon_not_configured: "خاصية تجربة اللبس مو جاهزة للحين.",
    product_not_found: "المنتج مو موجود في الكتالوج.",
    photo_too_large: "الصورة كبيرة مرّة، جرّب صورة أصغر (أقل من 8 ميغا).",
  };

  function friendlyError(code) {
    return ERROR_MESSAGES[code] || "صار خطأ، جرّب مرة ثانية.";
  }

  function sendMessage() {
    if (sendBtn.disabled) return;
    var text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    addUserMessage(text);
    history.push({ role: "user", content: text });
    tagShopifyCart();

    sendBtn.disabled = true;
    var typingEl = addTyping();

    fetch(API_BASE + "/widget/chat/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Token": STORE_KEY },
      body: JSON.stringify({
        messages: history,
        session_id: SESSION_ID,
        shown_skus: shownSkus,
        page_url: window.location.href,
      }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        typingEl.remove();
        if (!data.ok) {
          sendBtn.disabled = false;
          addErrorMessage(friendlyError(data.error));
          return;
        }
        // الكروت والخصم بيطلعوا بعد ما يخلص البوت كتابة الرد، متل محادثة حقيقية.
        function showExtras() {
          sendBtn.disabled = false;
          if (data.recommendations && data.recommendations.length) {
            data.recommendations.forEach(addRecommendationCard);
          } else if (data.recommendation) {
            addRecommendationCard(data.recommendation);
          }
          if (data.discount_offer) {
            addDiscountMessage(data.discount_offer);
          }
          var advice = data.size_advice;
          if (advice && advice.available && advice.sku) {
            markSize(advice.sku, advice.size);
            // كرت جديد تحت الرد بالمقاس وزر السلة — الكرت القديم غالباً طلع لفوق.
            var shownNow = (data.recommendations || []).some(function (r) {
              return r.sku === advice.sku;
            });
            if (advice.card && !shownNow) addRecommendationCard(advice.card);
            // المتجر بيقدر يختار المقاس بصفحته (متجر الديمو بيعمل هيك).
            try {
              window.dispatchEvent(new CustomEvent("control:size-recommended", {
                detail: { sku: advice.sku, size: advice.size, product_id: advice.card && advice.card.product_id },
              }));
            } catch (e) {}
          }
        }
        if (data.reply) {
          history.push({ role: "assistant", content: data.reply });
          addBotMessage(data.reply, showExtras);
        } else {
          showExtras();
        }
      })
      .catch(function () {
        typingEl.remove();
        sendBtn.disabled = false;
        addErrorMessage("ما قدرنا نوصل للمساعد، جرّب مرة ثانية.");
      });
  }

  sendBtn.addEventListener("click", sendMessage);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendMessage();
  });

  function runTryon(sku, photo) {
    var loadingEl = document.createElement("div");
    loadingEl.className = "typing";
    loadingEl.textContent = "نجهّز إطلالتك... ممكن تاخذ كم ثانية ⏳";
    msgsEl.appendChild(loadingEl);
    scrollToBottom();

    var form = new FormData();
    form.append("sku", sku);
    form.append("photo", photo);
    form.append("session_id", SESSION_ID);
    form.append("shopper_id", SHOPPER_ID);

    fetch(API_BASE + "/widget/tryon/", {
      method: "POST",
      headers: { "X-Api-Token": STORE_KEY },
      body: form,
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        loadingEl.remove();
        if (!data.ok) {
          if (data.error === "shopper_limit_reached") {
            disableTryons();
            addBotMessage(
              "خلصت تجارب اللبس في اشتراكك 🙏 جدّد اشتراكك عشان تحصل على تجارب جديدة، " +
                "وإلى ذاك الوقت أبشر، أساعدك تختار المقاس واللون اللي يناسبك 👌"
            );
            return;
          }
          addErrorMessage(friendlyError(data.error));
          return;
        }
        addTryonResultCard(data.image_url, null);
        addBotMessage("وش رأيك؟ عجبتك الإطلالة؟ 😊");
        if (data.shopper_remaining === 0) disableTryons();
        // للمتجر المضيف (متل عدّاد التجارب بمتجر الديمو)
        try {
          document.dispatchEvent(new CustomEvent("controlwidget:tryon", { detail: { remaining: data.shopper_remaining } }));
        } catch (e) {}
      })
      .catch(function () {
        loadingEl.remove();
        addErrorMessage("صار خطأ وقت تجهيز الإطلالة، جرّب مرة ثانية.");
      });
  }

  fileInput.addEventListener("change", function () {
    var file = fileInput.files[0];
    if (!file || !pendingSku) return;
    savedPhoto = file;
    runTryon(pendingSku, file);
  });
  // واجهة بسيطة للمتجر: زر "اعرف مقاسك" مثلاً بيفتح المحادثة وبيبعت رسالة جاهزة.
  window.ControlWidget = {
    open: function (message) {
      if (!panel.classList.contains("open")) openPanel();
      if (!message) return;
      // منستنى الترحيب يطلع قبل ما نبعت، حتى يضل ترتيب المحادثة طبيعي.
      setTimeout(function () {
        inputEl.value = message;
        sendMessage();
      }, 1800);
    },
  };
})();
