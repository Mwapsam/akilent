/*
 * The rule builder's two behaviours (Stage 5), loaded with the app shell so the pages that
 * use them swap in place (apps/core/htmx.py):
 *
 * - keywordChips(): a keyword field as chips. The real field is the plain comma-separated
 *   text input inside it (.chips-raw), which is all a page without scripts gets; with
 *   scripts it's hidden and kept in sync. Enter or a comma adds what you typed, × removes a
 *   chip, Backspace on an empty box takes the last one back to edit, and a paste is split on
 *   commas and new lines. Storage stays comma-separated, so a comma can't be inside a keyword:
 *   it always splits, and the hint under the field says so.
 *
 * - Drag to reorder: a list marked data-reorder holds rows marked data-reorder-item
 *   data-pk="…" with a handle marked data-reorder-handle, and one hidden form marked
 *   data-reorder-form (it carries the CSRF token and posts to the reorder view). Dropping a
 *   row posts the new order of pks as order=… and the page comes back renumbered. Every row
 *   also has Move up / Move down forms, which are the way in by keyboard, on a phone and
 *   without scripts; dragging is the shortcut for a mouse.
 */
(function () {
  "use strict";

  window.keywordChips = function () {
    return {
      chips: [],
      draft: "",
      raw: null,
      init: function () {
        this.raw = this.$el.querySelector(".chips-raw");
        if (!this.raw) return;
        this.chips = split(this.raw.value);
        this.raw.type = "hidden";
      },
      sync: function () {
        if (!this.raw) return;
        this.raw.value = this.chips.join(", ");
        this.raw.dispatchEvent(new Event("input", { bubbles: true }));
      },
      add: function (text) {
        var self = this;
        split(text === undefined ? this.draft : text).forEach(function (k) {
          var lower = k.toLowerCase();
          if (!self.chips.some(function (c) { return c.toLowerCase() === lower; })) self.chips.push(k);
        });
        this.draft = "";
        this.sync();
      },
      remove: function (i) {
        this.chips.splice(i, 1);
        this.sync();
      },
      key: function (e) {
        if (e.key === "Enter" || e.key === ",") {
          if (this.draft.trim()) { e.preventDefault(); this.add(); }
          else if (e.key === ",") e.preventDefault();
        } else if (e.key === "Backspace" && !this.draft && this.chips.length) {
          e.preventDefault();
          this.draft = this.chips.pop();
          this.sync();
        }
      },
      paste: function (e) {
        var text = (e.clipboardData || window.clipboardData).getData("text");
        if (/[,\n]/.test(text)) { e.preventDefault(); this.add(this.draft + text); }
      },
      blur: function () { if (this.draft.trim()) this.add(); },
    };
  };

  function split(text) {
    return String(text || "").split(/[,\n]/).map(function (s) { return s.trim(); }).filter(Boolean);
  }

  // ---- Drag to reorder ---------------------------------------------------------------------
  var dragged = null;

  function itemOf(el) { return el && el.closest ? el.closest("[data-reorder-item]") : null; }

  document.addEventListener("mousedown", function (e) {
    // Only the handle starts a drag, so text in a row can still be selected.
    var handle = e.target.closest && e.target.closest("[data-reorder-handle]");
    var item = itemOf(e.target);
    if (item) item.draggable = !!handle;
  });
  document.addEventListener("dragstart", function (e) {
    var item = itemOf(e.target);
    if (!item || !item.closest("[data-reorder]")) return;
    dragged = item;
    item.classList.add("is-dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", item.dataset.pk); } catch (err) { /* old browsers */ }
  });
  document.addEventListener("dragover", function (e) {
    var over = itemOf(e.target);
    if (!dragged || !over || over === dragged || over.parentNode !== dragged.parentNode) return;
    e.preventDefault();
    var box = over.getBoundingClientRect();
    var after = e.clientY > box.top + box.height / 2;
    over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
  });
  document.addEventListener("drop", function (e) {
    if (dragged) e.preventDefault();
  });
  document.addEventListener("dragend", function () {
    if (!dragged) return;
    var item = dragged;
    dragged = null;
    item.classList.remove("is-dragging");
    item.draggable = false;
    var list = item.closest("[data-reorder]");
    var form = list && list.querySelector("[data-reorder-form]");
    if (!form) return;
    var pks = Array.prototype.map.call(list.querySelectorAll("[data-reorder-item]"), function (el) {
      return el.dataset.pk;
    });
    if (pks.join() === (list.dataset.reorderInitial || "")) return; // dropped where it was
    form.querySelectorAll('input[name="order"]').forEach(function (i) { i.remove(); });
    pks.forEach(function (pk) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "order";
      input.value = pk;
      form.appendChild(input);
    });
    form.requestSubmit();
  });
  // Remember each list's order as rendered, so a drop in place doesn't post.
  document.addEventListener("htmx:load", function (e) {
    var root = e.detail && e.detail.elt;
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("[data-reorder]").forEach(function (list) {
      list.dataset.reorderInitial = Array.prototype.map.call(
        list.querySelectorAll("[data-reorder-item]"), function (el) { return el.dataset.pk; }
      ).join();
    });
  });
})();
