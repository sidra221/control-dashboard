(function () {
  "use strict";

  var scriptEl = document.currentScript;
  var STORE_KEY = scriptEl ? scriptEl.getAttribute("data-store-key") : null;
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

  var host = document.createElement("div");
  host.id = "sw-widget-host";
  host.style.all = "initial";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent = [
    ":host, * { box-sizing: border-box; }",
    ".bubble {",
    "  position: fixed; bottom: 20px; left: 20px; z-index: 999999;",
    "  width: 56px; height: 56px; border-radius: 50%;",
    "  background: linear-gradient(135deg, #6366f1, #4f46e5);",
    "  color: #fff; border: none; cursor: pointer;",
    "  box-shadow: 0 8px 24px -8px rgba(99,102,241,.6);",
    "  font-size: 24px; display: flex; align-items: center; justify-content: center;",
    "  font-family: system-ui, sans-serif;",
    "  transition: transform .15s ease;",
    "}",
    ".bubble:hover { transform: scale(1.06); }",
    ".panel {",
    "  position: fixed; bottom: 86px; left: 20px; z-index: 999999;",
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
    ".card { align-self: flex-start; max-width: 88%; background: #fff; border: 1px solid #e4e6ef; border-radius: 14px; padding: 10px; }",
    ".card img { width: 100%; border-radius: 10px; display: block; margin-bottom: 8px; max-height: 180px; object-fit: cover; }",
    ".card .title { font-size: 13px; font-weight: 700; margin-bottom: 2px; }",
    ".card .price { font-size: 12.5px; color: #6b7083; margin-bottom: 8px; }",
    ".card button, .card a.buy {",
    "  display: inline-block; text-decoration: none; text-align: center;",
    "  font-size: 12.5px; font-weight: 700; border-radius: 9px; padding: 8px 10px;",
    "  border: none; cursor: pointer; width: 100%; margin-top: 6px; font-family: inherit;",
    "}",
    ".card button.tryon { background: #eef0ff; color: #4f46e5; }",
    ".card a.buy { background: linear-gradient(135deg, #6366f1, #4f46e5); color: #fff; }",
    ".typing { align-self: flex-start; font-size: 12px; color: #9095ab; padding: 0 4px; }",
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
  ].join("\n");
  root.appendChild(style);

  var bubble = document.createElement("button");
  bubble.className = "bubble";
  bubble.innerHTML = "👗";
  bubble.title = "تحتاج مساعدة باختيار إطلالتك؟";
  root.appendChild(bubble);

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

  bubble.addEventListener("click", function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open") && history.length === 0) {
      addBotMessage("أهلاً! بدك مساعدة تلاقي إطلالتك المثالية؟ احكيلي شو بتدوّر عليه 💜");
    }
  });
  closeBtn.addEventListener("click", function () {
    panel.classList.remove("open");
  });

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

  function addBotMessage(text) {
    var el = document.createElement("div");
    el.className = "msg bot";
    el.textContent = text;
    msgsEl.appendChild(el);
    scrollToBottom();
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
    el.textContent = "عم يكتب...";
    msgsEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function addRecommendationCard(rec) {
    var card = document.createElement("div");
    card.className = "card";
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
    price.textContent = rec.price;
    card.appendChild(price);

    var tryBtn = document.createElement("button");
    tryBtn.className = "tryon";
    tryBtn.textContent = "جرّب شكلك فيه 📸";
    tryBtn.addEventListener("click", function () {
      pendingSku = rec.sku;
      fileInput.value = "";
      fileInput.click();
    });
    card.appendChild(tryBtn);

    if (rec.product_url) {
      var buyLink = document.createElement("a");
      buyLink.className = "buy";
      buyLink.href = rec.product_url;
      buyLink.target = "_blank";
      buyLink.rel = "noopener";
      buyLink.textContent = "اشتري الآن";
      card.appendChild(buyLink);
    }

    msgsEl.appendChild(card);
    scrollToBottom();
  }

  function addTryonResultCard(imageUrl, productUrl) {
    var card = document.createElement("div");
    card.className = "card";
    var img = document.createElement("img");
    img.src = imageUrl;
    img.alt = "شكلك فيه";
    card.appendChild(img);
    if (productUrl) {
      var buyLink = document.createElement("a");
      buyLink.className = "buy";
      buyLink.href = productUrl;
      buyLink.target = "_blank";
      buyLink.rel = "noopener";
      buyLink.textContent = "عجبني، اشتريه الآن";
      card.appendChild(buyLink);
    }
    msgsEl.appendChild(card);
    scrollToBottom();
  }

  var ERROR_MESSAGES = {
    invalid_token: "في مشكلة بإعداد المتجر — تواصل مع صاحب المتجر.",
    token_disabled: "الخدمة موقوفة مؤقتاً لهالمتجر.",
    quota_exceeded: "نفذ رصيد الصور المسموح لهالفترة.",
    assistant_not_configured: "المساعد الذكي مش جاهز بعد.",
    tryon_not_configured: "خاصية تجربة اللبس مش جاهزة بعد.",
    product_not_found: "المنتج مش موجود بالكتالوج.",
  };

  function friendlyError(code) {
    return ERROR_MESSAGES[code] || "صار في خطأ، جرّب كمان مرة.";
  }

  function sendMessage() {
    var text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    addUserMessage(text);
    history.push({ role: "user", content: text });

    sendBtn.disabled = true;
    var typingEl = addTyping();

    fetch(API_BASE + "/widget/chat/", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Token": STORE_KEY },
      body: JSON.stringify({ messages: history }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        typingEl.remove();
        sendBtn.disabled = false;
        if (!data.ok) {
          addErrorMessage(friendlyError(data.error));
          return;
        }
        if (data.reply) {
          addBotMessage(data.reply);
          history.push({ role: "assistant", content: data.reply });
        }
        if (data.recommendation) {
          addRecommendationCard(data.recommendation);
        }
      })
      .catch(function () {
        typingEl.remove();
        sendBtn.disabled = false;
        addErrorMessage("ما قدرنا نوصل للمساعد، جرّب كمان مرة.");
      });
  }

  sendBtn.addEventListener("click", sendMessage);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendMessage();
  });

  fileInput.addEventListener("change", function () {
    var file = fileInput.files[0];
    if (!file || !pendingSku) return;

    var loadingEl = document.createElement("div");
    loadingEl.className = "typing";
    loadingEl.textContent = "عم نركّب الإطلالة... ممكن ياخد كم ثانية ⏳";
    msgsEl.appendChild(loadingEl);
    scrollToBottom();

    var form = new FormData();
    form.append("sku", pendingSku);
    form.append("photo", file);

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
          addErrorMessage(friendlyError(data.error));
          return;
        }
        addTryonResultCard(data.image_url, null);
      })
      .catch(function () {
        loadingEl.remove();
        addErrorMessage("صار في خطأ وقت تركيب الإطلالة، جرّب كمان مرة.");
      });
  });
})();
