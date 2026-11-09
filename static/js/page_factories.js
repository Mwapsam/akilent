/*
 * Small page components loaded with the app shell (templates/base.html), so the pages that
 * use them swap in place instead of forcing a full page load for one <script> tag
 * (apps/core/htmx.py). Each defines a factory or an akilent.onPage hook that does nothing
 * on pages without its markup, and is safe to run on every navigation.
 *
 * - templatePicker(): Automations › "Follow up with a template": a live preview of what a
 *   named and a nameless customer would each receive (reads #tpl-data).
 * - chartSwitch(key, choices): Insights › a tab's metric switcher. Remembers the chosen chart
 *   for the tab in sessionStorage, so the page's quiet 5-minute refresh doesn't reset it.
 */
(function () {
  "use strict";

  window.chartSwitch = function (key, choices) {
    var stored = null;
    try { stored = window.sessionStorage.getItem("ak-chart:" + key); } catch (e) { /* private mode */ }
    return {
      // A remembered chart this page no longer has (no revenue this period) falls back.
      metric: choices.indexOf(stored) !== -1 ? stored : choices[0],
      pick: function (m) {
        this.metric = m;
        try { window.sessionStorage.setItem("ak-chart:" + key, m); } catch (e) { /* ignore */ }
      },
    };
  };

  window.templatePicker = function () {
    var el = document.getElementById("tpl-data");
    var data = el ? JSON.parse(el.textContent) : { templates: [], business: "" };
    var picks = {};
    data.templates.forEach(function (t) {
      t.blanks.forEach(function (b) { picks[t.pk + "__" + b.name] = b.default; });
    });
    return {
      tpl: "", picks: picks, lits: {},
      render: function (named) {
        var self = this;
        var t = data.templates.find(function (x) { return String(x.pk) === self.tpl; });
        if (!t) return "";
        var out = t.content;
        t.blanks.forEach(function (b) {
          var k = t.pk + "__" + b.name, p = self.picks[k], v;
          if (p === "first_name") v = named ? "Ada" : "there";
          else if (p === "full_name") v = named ? "Ada Mwape" : "there";
          else if (p === "phone") v = "+260 97 1234567";
          else if (p === "business") v = data.business;
          else v = self.lits[k] || "…";
          out = out.split("{" + "{" + b.name + "}" + "}").join(v);
        });
        return out;
      },
    };
  };
})();
