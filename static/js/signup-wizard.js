/*
 * Service-first signup wizard (see templates/accounts/signup.html).
 *
 * Exposes `window.signupWizard`, the Alpine factory behind
 * `x-data="signupWizard()"`, that walks a new customer through:
 *
 *   0. Select services   — Email / WhatsApp / Email & WhatsApp
 *   1. Select package    — plans filtered to the chosen services
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
      preselect: { services: "", plan: "", startStep: 0 },
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

        // Land on the step the server asked for (or the details step when a
        // submit bounced back with errors).
        var start = typeof pre.startStep === "number" ? pre.startStep : 0;
        this.step = hasErrors ? 2 : start;

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

      priceLabel: function (p) {
        if (!p || !p.priceMonthly) return "Free";
        return "$" + Math.round(p.priceMonthly) + "/mo";
      },
    };
  }

  window.signupWizard = signupWizard;
})();
