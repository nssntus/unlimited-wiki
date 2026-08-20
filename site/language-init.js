(function () {
  "use strict";

  var saved = null;
  try {
    saved = localStorage.getItem("unlimited-wiki-locale");
  } catch (_error) {
    saved = null;
  }

  var browserLanguage = navigator.language || "zh-CN";
  var locale = saved === "en" || saved === "zh-CN" ? saved : browserLanguage.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
  document.documentElement.lang = locale;
  document.documentElement.dataset.locale = locale;
})();
