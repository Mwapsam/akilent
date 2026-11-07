/*
 * Channel-first signup wizard (see templates/accounts/signup.html).
 *
 * Exposes `window.signupWizard`, the Alpine factory behind
 * `x-data="signupWizard()"`. Steps are named, and which ones show depends on how the
 * customer arrived (`entry`, worked out by the server):
 *
 *   "plans":  channels -> package -> account -> review   (a paid plan, channel links)
 *   "trial":  channels -> account -> review              (Start free trial, plain /signup/)
 *
 *   channels — any of WhatsApp, Instagram, Facebook Page, Email
 *   package  — plans that give every chosen channel, and the billing term
 *   account  — name, email, password, company (the real form fields)
 *   review   — confirm, then submit
 *
 * Config (plans, channels, where to open, any re-render form_data/errors) is read from
 * the #signup-wizard-config JSON script tag. Nothing here bypasses server validation:
 * the last step submits the same <form> to POST /signup/ and every field is a real
 * form control. The checks below only catch mistakes early, field by field.
 *
 * Progress (minus passwords) is kept in sessionStorage and the step in the URL
 * (?step=account), so a reload or the Back button doesn't start over.
 */
(function () {
  "use strict";

  var STORE_KEY = "akilent.signup.v1";
  var TITLES = { channels: "Channels", package: "Package", account: "Your account", review: "Review" };
  var ACCOUNT_FIELDS = ["first_name", "last_name", "email", "company_name", "password1", "password2"];
  var REQUIRED_MESSAGES = {
    first_name: "Enter your first name.",
    last_name: "Enter your last name.",
    email: "Enter your work email.",
    company_name: "Enter your company name.",
    password1: "Choose a password.",
    password2: "Type the password again.",
  };
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function readStore() {
    try {
      return JSON.parse(window.sessionStorage.getItem(STORE_KEY) || "null");
    } catch (e) {
      return null;
    }
  }

  function signupWizard() {
    var cfg = {
      plans: [],
      channelChoices: [],
      periods: [],
      currency: { symbol: "$", zeroDecimal: false },
      paymentMethods: [],
      preselect: { channels: [], plan: "", period: "monthly", entry: "plans", startStep: "channels", fromQuery: false },
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

    // Server-side errors, one message per field ({field: [{message}]} from Django).
    var serverErrors = {};
    Object.keys(cfg.errors || {}).forEach(function (key) {
      var list = cfg.errors[key] || [];
      if (list.length) serverErrors[key] = list.map(function (e) { return e.message; }).join(" ");
    });

    return {
      cfg: cfg,
      step: "channels",
      entry: pre.entry === "trial" ? "trial" : "plans",

      channels: (pre.channels && pre.channels.length ? pre.channels : fd.selected_channels || []).slice(),
      plan: pre.plan || fd.plan || "",
      // Billing term for a paid plan. The server works it out (a re-render's
      // posted value, else ?period=, else monthly) and prices each term with
      // billing.pricing.price_for, as checkout does.
      period: pre.period || "monthly",

      // A paid plan asks first (the "Create your account and pay" dialog):
      // submitting creates the account and goes straight on to payment.
      confirmOpen: false,

      fields: {
        first_name: fd.first_name || "",
        last_name: fd.last_name || "",
        email: fd.email || "",
        company_name: fd.company_name || "",
        password1: "",
        password2: "",
      },
      // A field shows its problem once the customer has left it (or tried to move on).
      touched: {},
      serverErrors: serverErrors,
      // Steps where Continue was pressed while something was missing.
      attempted: {},
      // Read out by the aria-live region: the step reached, or what needs fixing.
      announcement: "",

      init: function () {
        var self = this;
        var hasErrors = Object.keys(serverErrors).length > 0;
        var target = pre.startStep || "channels";

        if (hasErrors) {
          // A submit bounced back: open on the step that shows the first problem.
          if (serverErrors.selected_channels) target = "channels";
          else if (serverErrors.plan || serverErrors.billing_period) target = this.entry === "trial" ? "channels" : "package";
          else target = "account";
          ACCOUNT_FIELDS.forEach(function (k) { if (serverErrors[k]) self.touched[k] = true; });
        } else {
          target = this._restore(target);
        }

        // Guard against channel/plan drift: a preselected plan that no longer
        // gives every chosen channel sends the user back to pick again.
        if (this.plan && !this._planInList(this.plan)) {
          if (this.entry === "trial") this.channels = [];
          else this.plan = "";
        }

        this.step = this._firstIncomplete(target);
        this._setUrl(false);
        this._save();
        this._track("signup_step", { step: this.step });

        window.addEventListener("popstate", function () {
          var asked = new URLSearchParams(window.location.search).get("step") || self.steps[0];
          if (self.steps.indexOf(asked) === -1) asked = self.steps[0];
          self._show(self._firstIncomplete(asked), { push: false });
        });
      },

      // ---- Steps -------------------------------------------------------------

      get steps() {
        return this.entry === "trial"
          ? ["channels", "account", "review"]
          : ["channels", "package", "account", "review"];
      },
      get stepIndex() {
        return this.steps.indexOf(this.step);
      },
      title: function (key) {
        return TITLES[key] || key;
      },
      isLast: function () {
        return this.step === "review";
      },

      stepComplete: function (key) {
        if (key === "channels") return this.channels.length > 0 && this.filteredPlans.length > 0;
        if (key === "package") return !!this.plan && this._planInList(this.plan);
        if (key === "account") {
          var self = this;
          return ACCOUNT_FIELDS.every(function (k) {
            return !self._clientError(k) && !self.serverErrors[k];
          });
        }
        return true;
      },
      get canAdvance() {
        return this.stepComplete(this.step);
      },
      // The first step, up to `target`, that still needs something.
      _firstIncomplete: function (target) {
        var steps = this.steps;
        var end = steps.indexOf(target);
        if (end === -1) end = 0;
        for (var i = 0; i < end; i++) {
          if (!this.stepComplete(steps[i])) return steps[i];
        }
        return steps[end];
      },

      next: function () {
        if (!this.canAdvance) {
          this._blocked();
          return;
        }
        var i = this.stepIndex;
        if (i < this.steps.length - 1) this._show(this.steps[i + 1]);
      },
      prev: function () {
        var i = this.stepIndex;
        if (i > 0) this._show(this.steps[i - 1]);
      },
      // Steps already done can be revisited from the stepper or a Review "Edit" link.
      goTo: function (key) {
        var i = this.steps.indexOf(key);
        if (i !== -1 && i < this.stepIndex) this._show(key);
      },

      _show: function (key, opts) {
        opts = opts || {};
        this.step = key;
        if (opts.push !== false) this._setUrl(true);
        this._save();
        this._track("signup_step", { step: key });
        this.announcement = "Step " + (this.stepIndex + 1) + " of " + this.steps.length + ", " + this.title(key);
        var self = this;
        this.$nextTick(function () {
          var heading = self.$refs["heading_" + key];
          if (heading) heading.focus({ preventScroll: false });
        });
      },

      // Continue pressed with something missing: say what, and where.
      _blocked: function () {
        var self = this;
        this.attempted[this.step] = true;
        if (this.step === "account") {
          var bad = ACCOUNT_FIELDS.filter(function (k) {
            self.touched[k] = true;
            return self.fieldError(k);
          });
          this.announcement = bad.length === 1 ? "1 field needs attention." : bad.length + " fields need attention.";
          bad.forEach(function (k) { self._track("signup_error", { step: "account", field: k }); });
          this.$nextTick(function () {
            var first = bad.length && document.getElementById("id_" + bad[0]);
            if (first) first.focus();
          });
          return;
        }
        this.announcement = this.hint;
        this._track("signup_error", { step: this.step, field: this.step === "channels" ? "selected_channels" : "plan" });
      },
      // Shown under Continue once it was pressed with the step incomplete.
      get hint() {
        if (!this.attempted[this.step] || this.canAdvance) return "";
        if (this.step === "channels") {
          return this.channels.length
            ? "No single package includes all of these yet. Untick one to continue."
            : "Pick at least one channel to continue.";
        }
        if (this.step === "package") return "Choose a package to continue.";
        if (this.step === "account") return "Fix the highlighted fields to continue.";
        return "";
      },

      // "Compare all plans" from a trial signup: show the Package step.
      seeAllPlans: function () {
        this.entry = "plans";
        this._save();
      },

      // ---- Channels and plans ------------------------------------------------

      get trialPlan() {
        if (this.entry !== "trial") return null;
        var slug = this.plan;
        return this.cfg.plans.filter(function (p) { return p.slug === slug; })[0] || null;
      },
      // The server only sends channels that are on offer and that some plan gives. A
      // trial signup offers the channels the trial gives.
      get channelCards() {
        var trial = this.trialPlan;
        if (!trial) return this.cfg.channelChoices;
        return this.cfg.channelChoices.filter(function (c) {
          return trial.channels.indexOf(c.value) !== -1;
        });
      },
      hasChannel: function (value) {
        return this.channels.indexOf(value) !== -1;
      },
      // Plans that give every chosen channel.
      get filteredPlans() {
        var picks = this.channels;
        return this.cfg.plans.filter(function (p) {
          return picks.every(function (c) {
            return p.channels.indexOf(c) !== -1;
          });
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
      get channelLabel() {
        var picks = this.channels;
        return this.cfg.channelChoices
          .filter(function (c) {
            return picks.indexOf(c.value) !== -1;
          })
          .map(function (c) {
            return c.label;
          })
          .join(", ");
      },

      _planInList: function (slug) {
        return this.filteredPlans.some(function (p) {
          return p.slug === slug;
        });
      },

      toggleChannel: function (value) {
        if (this.hasChannel(value)) {
          this.channels = this.channels.filter(function (c) {
            return c !== value;
          });
        } else {
          this.channels = this.channels.concat([value]);
        }
        // Drop a package that doesn't give the new selection (a trial only offers
        // channels it gives, so it keeps its plan).
        if (this.plan && !this._planInList(this.plan) && this.entry !== "trial") this.plan = "";
        this._save();
      },
      selectPlan: function (slug) {
        this.plan = slug;
        this._save();
      },
      selectPeriod: function (value) {
        this.period = value;
        this._save();
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
      // What Review says about starting a free plan.
      get freeNote() {
        var p = this.selectedPlan;
        if (p && p.trialDays) return "Your " + p.trialDays + "-day free trial starts now — no card needed.";
        return "Your free plan starts now — no card needed.";
      },
      // What the dialog says about step 2, from the ways to pay that are on.
      get payNote() {
        var m = this.cfg.paymentMethods || [];
        if (!m.length) {
          return "Online payment isn't available right now. Contact support and we'll switch your plan on.";
        }
        if (m.length > 1) return "You choose how to pay on the next page.";
        if (m[0].code === "manual") return "By bank transfer. We'll show you the details next.";
        return "On our payment provider's secure page.";
      },

      // ---- Account fields ----------------------------------------------------

      _clientError: function (name) {
        var f = this.fields;
        var value = (f[name] || "").trim();
        if (!value) return REQUIRED_MESSAGES[name] || "This field is required.";
        if (name === "email" && !EMAIL_RE.test(value)) return "Enter a valid email address, like name@company.com.";
        if (name === "password1") {
          if (f.password1.length < 8) return "Use at least 8 characters.";
          if (/^\d+$/.test(f.password1)) return "Use some letters or symbols, not only numbers.";
        }
        if (name === "password2" && f.password2 !== f.password1) return "The passwords don't match.";
        return "";
      },
      // What to show under a field: the server's word on it until it's edited, else
      // our own check once the field has been left.
      fieldError: function (name) {
        if (this.serverErrors[name]) return this.serverErrors[name];
        return this.touched[name] ? this._clientError(name) : "";
      },
      touch: function (name) {
        // A field left blank on the way through isn't flagged until Continue.
        if ((this.fields[name] || "") !== "") this.touched[name] = true;
      },
      edited: function (name) {
        if (this.serverErrors[name]) delete this.serverErrors[name];
        // The confirmation is re-checked live once it has been touched.
        if (name === "password1" && this.serverErrors.password2) delete this.serverErrors.password2;
      },
      // The password rules, ticked off as the customer types.
      get passwordRules() {
        var p1 = this.fields.password1;
        var p2 = this.fields.password2;
        return [
          { label: "8+ characters", ok: p1.length >= 8 },
          { label: "Not only numbers", ok: p1.length > 0 && !/^\d+$/.test(p1) },
          { label: "Passwords match", ok: p2.length > 0 && p1 === p2 },
        ];
      },

      // ---- Progress, URL and analytics ----------------------------------------

      // Pick up a reload or a return within this tab. Passwords are never kept, so a
      // restored signup reopens at Your account at the latest. Returns the step to aim
      // for: never earlier than where the server opened.
      _restore: function (target) {
        var saved = readStore();
        if (!saved || typeof saved !== "object") return target;
        var self = this;
        if (!pre.fromQuery) {
          var offered = this.cfg.channelChoices.map(function (c) { return c.value; });
          if (Array.isArray(saved.channels)) {
            this.channels = saved.channels.filter(function (c) { return offered.indexOf(c) !== -1; });
          }
          var plan = this.cfg.plans.filter(function (p) { return p.slug === saved.plan; })[0];
          if (plan) {
            this.plan = plan.slug;
            this.entry = saved.entry === "trial" && !this.isPaid(plan) ? "trial" : "plans";
          } else if (saved.entry === "plans") {
            this.entry = "plans";
            this.plan = "";
          }
          if (saved.period) this.period = saved.period;
        }
        if (saved.fields && typeof saved.fields === "object") {
          Object.keys(this.fields).forEach(function (k) {
            if (k.indexOf("password") === 0) return;
            if (!self.fields[k] && typeof saved.fields[k] === "string") self.fields[k] = saved.fields[k];
          });
        }
        var steps = this.steps;
        if (steps.indexOf(target) === -1) target = steps[0];
        if (steps.indexOf(saved.step) > steps.indexOf(target)) target = saved.step;
        return target;
      },
      _save: function () {
        try {
          var f = this.fields;
          window.sessionStorage.setItem(STORE_KEY, JSON.stringify({
            step: this.step,
            entry: this.entry,
            channels: this.channels,
            plan: this.plan,
            period: this.period,
            fields: { first_name: f.first_name, last_name: f.last_name, email: f.email, company_name: f.company_name },
          }));
        } catch (e) {
          /* private mode or storage blocked: the wizard still works, it just won't resume */
        }
      },
      _clearSaved: function () {
        try {
          window.sessionStorage.removeItem(STORE_KEY);
        } catch (e) {
          /* nothing to clear */
        }
      },
      _setUrl: function (push) {
        try {
          var url = new URL(window.location.href);
          url.searchParams.set("step", this.step);
          if (push) window.history.pushState({ step: this.step }, "", url);
          else window.history.replaceState({ step: this.step }, "", url);
        } catch (e) {
          /* very old browser: the step just isn't in the URL */
        }
      },
      // GA4 (templates/analytics/google.html). Names and steps only, never what was typed.
      _track: function (name, params) {
        if (typeof window.gtag !== "function") return;
        try {
          window.gtag("event", name, Object.assign({ plan: this.plan || "", entry: this.entry }, params));
        } catch (e) {
          /* analytics must never break signup */
        }
      },

      // ---- Submit ------------------------------------------------------------

      // The form's submit handler. Enter in a field submits the form too
      // (through the hidden Create button), so on the earlier steps it moves
      // on instead, and only the dialog's own button confirms a paid plan.
      submit: function (e) {
        if (!this.isLast()) {
          e.preventDefault();
          this.next();
          return;
        }
        // Browsers without SubmitEvent.submitter: the dialog being open is
        // the nearest sign its button was used.
        var confirmed = e.submitter
          ? e.submitter === this.$refs.payConfirm
          : this.confirmOpen;
        if (this.isPaid(this.selectedPlan) && !confirmed) {
          e.preventDefault();
          this.confirmOpen = true;
          return;
        }
        this.confirmOpen = false;
        // A bounced submit re-renders with everything but the passwords, and saves
        // again from there.
        this._clearSaved();
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
