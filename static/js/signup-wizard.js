/*
 * Service-first signup wizard (see templates/accounts/signup.html).
 *
 * Exposes `window.signupWizard`, the Alpine factory behind
 * `x-data="signupWizard()"`, that walks a new customer through:
 *
 *   0. Select services   — Email / WhatsApp / Email & WhatsApp
 *   1. Select package    — plans filtered to the chosen services, and the
 *                          billing term (monthly, yearly, ...) for a paid plan
 *   2. Your details      — personal + business info (the real form fields)
 *   3. Review            — confirm, then submit
 *
 * Config (plans, whether WhatsApp is enabled, CSRF token, where the wizard
 * should open, and any re-render form_data/errors) is read from the
 * #signup-wizard-config JSON script tag. Nothing here bypasses server
 * validation: the last step submits the same <form> to POST /signup/ and
 * every field is a real form control. Step 2's required fields are mirrored
 * into `fields` (via x-model) purely so "Continue" can be gated client-side
 * and Review can echo them back — the server still re-validates everything.
 */
(function () {
  "use strict";

  function signupWizard() {
    var cfg = {
      plans: [],
      serviceChoices: [],
      periods: [],
      currency: { symbol: "$", zeroDecimal: false },
      preselect: { services: "", plan: "", period: "monthly", startStep: 0 },
      formData: {},
      errors: {},
    };
    var el = document.getElementById("signup-wizard-config");
    if (el) {
      try {
        cfg = Object.assign(cfg, JSON.parse(el.textContent || "{}"));
      } catch (e) {
        /* keep defaults */
      }
    }
    var fd = cfg.formData || {};
    var pre = cfg.preselect || {};

    return {
      cfg: cfg,
      step: 0,
      stepLabels: ["Services", "Package", "Your details", "Review"],

      services: pre.services || fd.selected_services || "",
      plan: pre.plan || fd.plan || "",
      // Billing term for a paid plan. The server works it out (a re-render's
      // posted value, else ?period=, else monthly) and prices each term with
      // billing.pricing.price_for, as checkout does.
      period: pre.period || "monthly",

      // A paid plan asks first (the "Create your account and pay" dialog):
      // submitting creates the account and goes straight on to payment.
      confirmOpen: false,
      confirmed: false,

      // Step 2 ("Your details") fields, bound via x-model so "Continue" can
      // be gated the same way steps 0/1 are, and Review can echo them back.
      fields: {
        first_name: fd.first_name || "",
        last_name: fd.last_name || "",
        email: fd.email || "",
        phone: fd.phone || "",
        password1: "",
        password2: "",
        company_name: fd.company_name || "",
        address_line1: fd.address_line1 || "",
        city: fd.city || "",
        country: fd.country || "",
      },
      step2Required: [
        "first_name", "last_name", "email", "phone",
        "password1", "password2", "company_name",
        "address_line1", "city", "country",
      ],

      init: function () {
        var errs = cfg.errors || {};
        var hasErrors = errs && Object.keys(errs).length > 0;

        // Land on the step the server asked for, or, when a submit bounced back
        // with errors, on the step that shows the first of them.
        var start = typeof pre.startStep === "number" ? pre.startStep : 0;
        if (!hasErrors) this.step = start;
        else if (errs.selected_services) this.step = 0;
        else if (errs.plan || errs.billing_period) this.step = 1;
        else this.step = 2;

        // Guard against service/plan drift: a preselected plan that no longer
        // matches the chosen services sends the user back to pick again.
        if (this.plan && !this._planInList(this.plan)) {
          this.plan = "";
          if (this.step > 1) this.step = this.services ? 1 : 0;
        }
      },

      // The server only sends services that are on and have at least one plan.
      get serviceCards() {
        return this.cfg.serviceChoices;
      },
      get filteredPlans() {
        var s = this.services;
        return this.cfg.plans.filter(function (p) {
          return !s || p.serviceType === s;
        });
      },
      get selectedPlan() {
        var slug = this.plan;
        return (
          this.filteredPlans.filter(function (p) {
            return p.slug === slug;
          })[0] || null
        );
      },
      get serviceLabel() {
        var s = this.services;
        var hit = this.cfg.serviceChoices.filter(function (c) {
          return c.value === s;
        })[0];
        return hit ? hit.label : "";
      },

      _planInList: function (slug) {
        return this.filteredPlans.some(function (p) {
          return p.slug === slug;
        });
      },

      selectService: function (value) {
        if (this.services !== value) {
          this.services = value;
          // Drop a package that belonged to the previous selection.
          if (this.plan && !this._planInList(this.plan)) this.plan = "";
        }
      },
      selectPlan: function (slug) {
        this.plan = slug;
      },
      selectPeriod: function (value) {
        this.period = value;
      },

      // The term toggle only matters when a paid plan is on offer.
      get showPeriods() {
        return this.cfg.periods.length > 1 && this.filteredPlans.some(function (p) {
          return p.priceMonthly > 0;
        });
      },
      get periodInfo() {
        var v = this.period;
        return this.cfg.periods.filter(function (o) { return o.value === v; })[0] ||
          { value: "monthly", label: "Monthly", per: "month", months: 1, savePct: 0 };
      },
      isPaid: function (p) {
        return !!(p && p.priceMonthly > 0);
      },

      get canAdvance() {
        if (this.step === 0) return !!this.services;
        if (this.step === 1) return !!this.plan && this._planInList(this.plan);
        if (this.step === 2) {
          var f = this.fields;
          return this.step2Required.every(function (key) {
            return !!(f[key] && f[key].trim());
          });
        }
        return true;
      },

      next: function () {
        if (this.canAdvance && this.step < this.stepLabels.length - 1) this.step++;
      },
      prev: function () {
        if (this.step > 0) this.step--;
      },
      goTo: function (i) {
        if (i < this.step) this.step = i;
      },

      // The form's submit handler. Enter in a field submits too, so the
      // dialog is opened here rather than from the button.
      submit: function (e) {
        if (this.isPaid(this.selectedPlan) && !this.confirmed) {
          e.preventDefault();
          this.confirmOpen = true;
          return;
        }
        this.confirmed = false;
        // One submit at a time; app.js re-enables these if the page is
        // restored from the back/forward cache, and they unlock after 15s.
        e.target.querySelectorAll("[type=submit]").forEach(function (b) {
          b.disabled = true;
          b.setAttribute("data-submit-lock", "");
          setTimeout(function () {
            b.disabled = false;
            b.removeAttribute("data-submit-lock");
          }, 15000);
        });
      },

      _money: function (amount) {
        var cur = this.cfg.currency || {};
        var whole = cur.zeroDecimal || Math.round(amount * 100) % 100 === 0;
        var digits = whole ? 0 : 2;
        return (cur.symbol || "$") + amount.toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        });
      },
      // What one term costs, e.g. "$499.80 / year".
      priceLabel: function (p) {
        if (!this.isPaid(p)) return "Free";
        var info = this.periodInfo;
        var total = (p.periodPrices && p.periodPrices[info.value]) || p.priceMonthly;
        return this._money(total) + " / " + info.per;
      },
      // The term's price as a sentence: "$1,009.80 for one year". Checkout takes
      // any account credit off this, so the review says "up to".
      dueLabel: function (p) {
        if (!this.isPaid(p)) return "";
        var info = this.periodInfo;
        var total = (p.periodPrices && p.periodPrices[info.value]) || p.priceMonthly;
        var span = /^\d/.test(info.per) ? info.per : "one " + info.per;
        return this._money(total) + " for " + span;
      },
      // For terms longer than a month: the monthly equivalent, e.g. "$41.65/mo".
      perMonthLabel: function (p) {
        var info = this.periodInfo;
        var total = p && p.periodPrices && p.periodPrices[info.value];
        if (!this.isPaid(p) || info.months <= 1 || !total) return "";
        return this._money(total / info.months) + "/mo";
      },
    };
  }

  window.signupWizard = signupWizard;
})();
