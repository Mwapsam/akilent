/* Hover layer for server-rendered line charts.
 *
 * The chart itself is already on screen before this runs — it is SVG in the
 * HTML, not something drawn by JavaScript. All this adds is the crosshair and
 * the tooltip, so with JS off you lose a tooltip and keep the chart (and the
 * "View as table" disclosure underneath it, which is where the exact numbers
 * live anyway).
 *
 * Reads its data from the <script type="application/json"> island Django's
 * json_script writes, so the numbers in the tooltip are the same objects the
 * table renders — they cannot drift apart.
 */
(function () {
  "use strict";

  window.chartHover = function (uid) {
    return {
      active: null,
      columns: [],

      init() {
        const island = document.getElementById(uid);
        if (!island) return;
        try {
          this.columns = JSON.parse(island.textContent) || [];
        } catch (e) {
          // A malformed island should cost the tooltip, not the page.
          this.columns = [];
        }

        // Keyboard access: the chart is a group, so arrow keys walk the
        // columns once it has focus. Without this the tooltip is mouse-only,
        // which makes the precise values unreachable for anyone not using one
        // — the table view is the fallback, but this is cheaper than making
        // them go find it.
        this.$el.setAttribute("tabindex", "0");
        this.$el.addEventListener("keydown", (e) => {
          if (!this.columns.length) return;
          if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            const step = e.key === "ArrowRight" ? 1 : -1;
            const next = this.active === null ? 0 : this.active + step;
            this.active = Math.max(0, Math.min(next, this.columns.length - 1));
          } else if (e.key === "Escape") {
            this.active = null;
          }
        });
        this.$el.addEventListener("blur", () => { this.active = null; });
      },

      /* Follow the hovered column, but keep the box inside the plot: a tooltip
       * for the last day would otherwise hang off the right edge, which on a
       * phone means it is simply not readable. */
      tooltipStyle() {
        if (this.active === null || !this.columns[this.active]) return "display:none";
        const total = this.columns.length;
        const ratio = total > 1 ? this.active / (total - 1) : 0.5;
        const past_half = ratio > 0.5;
        const offset = (ratio * 100).toFixed(2);
        return (
          "left:" + offset + "%;top:0;" +
          "transform:translate(" + (past_half ? "calc(-100% - 8px)" : "8px") + ",0)"
        );
      },
    };
  };
})();
