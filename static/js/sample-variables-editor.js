/*
 * Turns the hidden #tpl-sample-variables JSON textarea into a flat
 * path/value table (dotted paths reconstruct into nested objects on save,
 * matching apps/email/services/render.py's flatten_variable_paths shape).
 * The textarea stays the source of truth — edits in the table just rewrite
 * its value and dispatch an "input" event, so every listener already bound
 * to it (live preview, autosave, draft backup, raw-mode undo stack) keeps
 * working unchanged. A "raw JSON" toggle stays available for edits the flat
 * table can't express well (e.g. array values).
 */
(function () {
  "use strict";

  function flatten(obj, prefix) {
    var rows = [];
    Object.keys(obj || {}).forEach(function (key) {
      var path = prefix ? prefix + "." + key : key;
      var value = obj[key];
      if (value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length) {
        rows = rows.concat(flatten(value, path));
      } else {
        rows.push({ path: path, value: value });
      }
    });
    return rows;
  }

  function unflatten(rows) {
    var result = {};
    rows.forEach(function (row) {
      if (!row.path) return;
      var parts = row.path.split(".");
      var node = result;
      for (var i = 0; i < parts.length - 1; i++) {
        var part = parts[i];
        if (typeof node[part] !== "object" || node[part] === null || Array.isArray(node[part])) {
          node[part] = {};
        }
        node = node[part];
      }
      node[parts[parts.length - 1]] = row.value;
    });
    return result;
  }

  function init() {
    var textarea = document.getElementById("tpl-sample-variables");
    if (!textarea) return;

    var wrap = document.createElement("div");
    wrap.className = "grid gap-2";

    var toggleLink = document.createElement("button");
    toggleLink.type = "button";
    toggleLink.className = "text-xs text-brand-600 font-medium mt-2 hover:underline";

    textarea.insertAdjacentElement("afterend", toggleLink);
    textarea.insertAdjacentElement("afterend", wrap);

    var rows = [];
    try {
      rows = flatten(JSON.parse(textarea.value || "{}"));
    } catch (e) {
      rows = [];
    }

    function sync() {
      textarea.value = JSON.stringify(unflatten(rows), null, 2);
      textarea.dispatchEvent(new Event("input"));
    }

    function render() {
      wrap.innerHTML = "";
      rows.forEach(function (row, i) {
        var line = document.createElement("div");
        line.className = "flex items-center gap-2";

        var pathInput = document.createElement("input");
        pathInput.type = "text";
        pathInput.className = "input font-mono sm:text-xs"; // 16px on phones (no iOS zoom), as .input is
        pathInput.placeholder = "contact.first_name";
        pathInput.value = row.path;
        pathInput.setAttribute("aria-label", "Variable path");
        pathInput.addEventListener("input", function () {
          row.path = pathInput.value;
          sync();
        });

        var valueInput = document.createElement("input");
        valueInput.type = "text";
        valueInput.className = "input sm:text-xs";
        valueInput.placeholder = "Ada";
        valueInput.value = row.value == null ? "" : row.value;
        valueInput.setAttribute("aria-label", "Sample value for " + (row.path || "variable"));
        valueInput.addEventListener("input", function () {
          row.value = valueInput.value;
          sync();
        });

        var removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "btn btn-ghost btn-sm shrink-0";
        removeBtn.textContent = "Remove";
        removeBtn.setAttribute("aria-label", "Remove variable " + (row.path || ""));
        removeBtn.addEventListener("click", function () {
          rows.splice(i, 1);
          render();
          sync();
        });

        line.appendChild(pathInput);
        line.appendChild(valueInput);
        line.appendChild(removeBtn);
        wrap.appendChild(line);
      });

      var addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "btn btn-secondary btn-sm mt-1";
      addBtn.textContent = "Add variable";
      addBtn.addEventListener("click", function () {
        rows.push({ path: "", value: "" });
        render();
      });
      wrap.appendChild(addBtn);
    }

    var showingRaw = false;
    function setMode(raw) {
      showingRaw = raw;
      textarea.classList.toggle("hidden", !raw);
      wrap.classList.toggle("hidden", raw);
      toggleLink.textContent = raw ? "Edit as table" : "Edit as raw JSON";
      if (!raw) {
        try {
          rows = flatten(JSON.parse(textarea.value || "{}"));
        } catch (e) {
          /* invalid JSON left by the raw editor — keep last-good rows */
        }
        render();
      }
    }

    render();
    setMode(false);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
