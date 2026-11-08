/*
 * "Start from a sample" picker on the create-template form. Reads starter
 * content from #starter-template-picker's data-starters attribute (see
 * templates_list view / apps.email.starter_templates) and prefills the
 * Name/Subject/Text body/HTML body fields on click — the user still reviews
 * and clicks "Create template" themselves, nothing is submitted here.
 */
(function () {
  "use strict";

  function init() {
    var picker = document.getElementById("starter-template-picker");
    var buttonRow = document.getElementById("starter-template-buttons");
    if (!picker || !buttonRow || buttonRow.dataset.startersInit) return;
    buttonRow.dataset.startersInit = "1";

    var starters;
    try {
      starters = JSON.parse(picker.getAttribute("data-starters") || "[]");
    } catch (e) {
      return;
    }
    if (!starters.length) return;

    var nameEl = document.getElementById("id_name");
    var subjectEl = document.getElementById("id_subject");
    var textEl = document.getElementById("id_text_body");
    var htmlEl = document.getElementById("id_html_body");

    starters.forEach(function (starter) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-secondary btn-sm";
      btn.textContent = starter.name;
      btn.addEventListener("click", function () {
        if (nameEl) nameEl.value = starter.name;
        if (subjectEl) subjectEl.value = starter.subject;
        if (textEl) textEl.value = starter.text_body;
        if (htmlEl) htmlEl.value = starter.html_body;
        if (nameEl) nameEl.focus();
      });
      buttonRow.appendChild(btn);
    });
  }

  // Loaded with the shell (templates/base.html): runs on every page, in place or not, and
  // does nothing where there's no picker.
  if (window.akilent && window.akilent.onPage) window.akilent.onPage(init);
  else document.addEventListener("DOMContentLoaded", init);
})();
