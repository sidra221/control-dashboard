// سكربت تطبيق زد — بينحط مرة وحدة بخانة Custom Snippets ببوابة الشركاء (جوا
// <script>…</script>)، وزد بتحطه بكل صفحات المتاجر اللي مفعّلة التطبيق. نفسه
// لكل المتاجر: بيبعت دومين المتجر لسيرفرنا، بياخد مفتاح الودجت، وبيحمّل embed.js.
(function () {
  var API_BASE = "https://control.datarecovery-sa.com";
  var LOADER_ID = "control-shopping-widget";
  if (document.getElementById(LOADER_ID)) return;

  function boot() {
    if (document.getElementById(LOADER_ID)) return;
    fetch(API_BASE + "/widget/zid-key/?host=" + encodeURIComponent(window.location.host))
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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
