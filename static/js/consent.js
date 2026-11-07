/*
 * Analytics consent (templates/components/cookie_consent.html).
 *
 * Google Analytics loads only after the visitor clicks Accept (templates/analytics/google.html
 * listens for "akilent:analytics-consent"). The choice is kept for a year in the
 * analytics_consent cookie ("granted" or "denied"); until there is one, every
 * [data-consent-bar] on the page is shown. [data-consent-reset] (the cookie policy) clears the
 * choice and shows the bar again. Plain JS, so it works on every shell, with or without Alpine.
 */
(function () {
  "use strict";

  var NAME = "analytics_consent";

  function read() {
    var m = document.cookie.match(/(?:^|; )analytics_consent=(granted|denied)/);
    return m ? m[1] : null;
  }

  function write(value, maxAge) {
    document.cookie =
      NAME + "=" + value + ";path=/;max-age=" + maxAge + ";SameSite=Lax" +
      (location.protocol === "https:" ? ";Secure" : "");
  }

  function bars() {
    return document.querySelectorAll("[data-consent-bar]");
  }

  function show() {
    bars().forEach(function (bar) { bar.hidden = false; });
  }

  function choose(value) {
    write(value, 31536000);
    bars().forEach(function (bar) { bar.hidden = true; });
    if (value === "granted") window.dispatchEvent(new Event("akilent:analytics-consent"));
  }

  function init() {
    if (!read()) show();
    document.addEventListener("click", function (e) {
      var pick = e.target.closest && e.target.closest("[data-consent]");
      if (pick) {
        choose(pick.getAttribute("data-consent") === "granted" ? "granted" : "denied");
        return;
      }
      var reset = e.target.closest && e.target.closest("[data-consent-reset]");
      if (reset) {
        e.preventDefault();
        write("", 0);
        show();
      }
    });
  }

  window.akilentConsent = { value: read };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
