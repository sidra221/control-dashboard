// سكربت تطبيق سلة (App Snippet) — بينحط مرة وحدة ببوابة شركاء سلة، وسلة بتحطه
// بكل صفحات المتاجر اللي مركّبة التطبيق. نفسه لكل المتاجر: بيعرف رقم المتجر،
// بيسأل سيرفرنا عن مفتاح الودجت، وبيحمّل embed.js. لازم يضل JS صافي (بلا HTML).
(function () {
  var API_BASE = "https://control.datarecovery-sa.com";
  var LOADER_ID = "control-shopping-widget";

  function boot() {
    if (document.getElementById(LOADER_ID)) return;
    var storeId = window.salla && salla.config && salla.config.get("store.id", "");
    if (!storeId) return;
    fetch(API_BASE + "/widget/salla-key/" + encodeURIComponent(storeId) + "/")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || !data.ok || document.getElementById(LOADER_ID)) return;
        var script = document.createElement("script");
        script.id = LOADER_ID;
        script.src = API_BASE + "/static/apitokens/embed.js";
        script.setAttribute("data-store-key", data.store_key);
        document.body.appendChild(script);
      })
      .catch(function () {});
  }

  // إعدادات المتجر ما بتكون جاهزة إلا بعد salla.onReady.
  if (window.salla && typeof salla.onReady === "function") {
    salla.onReady(boot);
  } else {
    window.addEventListener("load", boot);
  }
})();
