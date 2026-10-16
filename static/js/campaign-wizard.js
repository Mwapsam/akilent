/*
 * Guided bulk-campaign wizard (see templates/email/campaigns.html).
 *
 * Exposes `window.campaignWizard`, the Alpine factory behind
 * `x-data="campaignWizard()"`, that walks a non-technical user through four
 * steps — Message, Recipients, Sender, Review — instead of the old single
 * raw form. Config (verified domains, saved templates, the
 * send-test URL, CSRF token, and any re-render form_data/errors) is read from
 * the #campaign-wizard-config JSON script tag.
 *
 * Nothing here bypasses server validation: the final step submits the same
 * <form> to POST /email/campaigns/create/, and every field is a real form
 * control. Disabled controls (the paths the user didn't pick) simply aren't
 * submitted, which is how recipient-source precedence is enforced.
 */
(function () {
  "use strict";

  var EMAIL_RE = /[^\s,;<>"']+@[^\s,;<>"']+\.[^\s,;<>"']+/g;

  function parseEmails(raw) {
    var seen = Object.create(null);
    var out = [];
    var matches = String(raw || "").match(EMAIL_RE) || [];
    matches.forEach(function (m) {
      var e = m.trim().toLowerCase();
      if (e && !seen[e]) {
        seen[e] = true;
        out.push(e);
      }
    });
    return out;
  }

  // Small CSV reader for the in-browser preview only. The server re-parses the
  // uploaded file with the stdlib csv module — this just needs to be good
  // enough to show a row count and detected columns.
  function parseCsv(text) {
    var lines = String(text || "")
      .replace(/\r\n?/g, "\n")
      .split("\n")
      .filter(function (l) {
        return l.trim() !== "";
      });
    if (lines.length < 2) return { headers: [], rows: [], emailKey: null, variables: [] };

    var split = function (line) {
      return line.split(",").map(function (c) {
        return c.trim().replace(/^"|"$/g, "");
      });
    };
    var headers = split(lines[0]);
    var lower = headers.map(function (h) {
      return h.toLowerCase();
    });
    var emailKey =
      lower.indexOf("to") !== -1
        ? headers[lower.indexOf("to")]
        : lower.indexOf("email") !== -1
        ? headers[lower.indexOf("email")]
        : headers[0];

    var rows = lines.slice(1).map(function (line) {
      var cells = split(line);
      var row = {};
      headers.forEach(function (h, i) {
        row[h] = cells[i] || "";
      });
      return row;
    });
    rows = rows.filter(function (r) {
      return (r[emailKey] || "").indexOf("@") !== -1;
    });

    var variables = headers.filter(function (h) {
      return h !== emailKey;
    });
    return { headers: headers, rows: rows, emailKey: emailKey, variables: variables };
  }

  function campaignWizard() {
    var cfg = { verifiedDomains: [], templates: [], formData: {}, errors: {} };
    var el = document.getElementById("campaign-wizard-config");
    if (el) {
      try {
        cfg = Object.assign(cfg, JSON.parse(el.textContent || "{}"));
      } catch (e) {
        /* keep defaults */
      }
    }
    var fd = cfg.formData || {};

    return {
      cfg: cfg,
      step: 0,
      stepLabels: ["Message", "Recipients", "From", "Review"],

      // Step 1 — message
      mode: fd.mode === "template" ? "template" : "text",
      subject: fd.subject || "",
      message: fd.text_body || "",
      templateId: fd.template_id ? String(fd.template_id) : "",

      // Step 2 — recipients
      recipientMethod: "csv",
      csv: { rows: [], variables: [], emailKey: null },
      csvError: "",
      pasteText: fd.recipients_text || "",
      jsonText: fd.recipients_json || "",

      // Step 3 — sender
      fromLocal: fd.from_local || "hello",
      fromDomain: fd.from_domain || (cfg.verifiedDomains[0] || ""),

      testing: false,
      submitting: false,
      _confirmed: false,

      init: function () {
        if (fd.recipients_text) this.recipientMethod = "paste";
        else if (fd.recipients_json) this.recipientMethod = "json";

        var errs = cfg.errors || {};
        if (errs.recipients) this.step = 1;
        else if (errs.from_email) this.step = 2;
      },

      get hasDomains() {
        return this.cfg.verifiedDomains.length > 0;
      },
      get fromEmail() {
        var local = (this.fromLocal || "").trim().replace(/@.*$/, "");
        return local && this.fromDomain ? local + "@" + this.fromDomain : "";
      },
      get selectedTemplate() {
        var id = String(this.templateId);
        return (
          this.cfg.templates.filter(function (t) {
            return String(t.id) === id;
          })[0] || null
        );
      },
      get mergeFields() {
        if (this.mode === "template") {
          return this.selectedTemplate ? this.selectedTemplate.variables : [];
        }
        return this.csv.variables || [];
      },
      get recipientCount() {
        if (this.recipientMethod === "csv") return this.csv.rows.length;
        if (this.recipientMethod === "paste") return parseEmails(this.pasteText).length;
        try {
          var parsed = JSON.parse(this.jsonText || "[]");
          return parsed.filter(function (r) {
            return r && r.to;
          }).length;
        } catch (e) {
          return 0;
        }
      },
      get contentReady() {
        if (this.mode === "template") return !!this.templateId;
        return this.subject.trim() !== "" && this.message.trim() !== "";
      },
      get canAdvance() {
        if (this.step === 0) return this.contentReady;
        if (this.step === 1) return this.recipientCount > 0;
        if (this.step === 2) return this.hasDomains && !!this.fromEmail;
        return true;
      },

      next: function () {
        if (this.canAdvance && this.step < this.stepLabels.length - 1) this.step++;
      },
      prev: function () {
        if (this.step > 0) this.step--;
      },

      selectMethod: function (m) {
        this.recipientMethod = m;
        if (m !== "csv" && this.$refs.csvInput) {
          this.$refs.csvInput.value = "";
          this.csv = { rows: [], variables: [], emailKey: null };
          this.csvError = "";
        }
      },

      onCsvChange: function (event) {
        var file = event.target.files && event.target.files[0];
        this.csv = { rows: [], variables: [], emailKey: null };
        this.csvError = "";
        if (!file) return;
        var self = this;
        var reader = new FileReader();
        reader.onload = function () {
          var parsed = parseCsv(reader.result);
          if (!parsed.rows.length) {
            self.csvError =
              "No recipients found. The file needs a header row with a 'to' column plus one row per person.";
            return;
          }
          self.csv = parsed;
        };
        reader.onerror = function () {
          self.csvError = "That file couldn't be read. Try re-exporting it as CSV.";
        };
        reader.readAsText(file);
      },

      insertField: function (name) {
        var ref = this.$refs.messageInput;
        var token = "{{ " + name + " }}";
        if (!ref) {
          this.message += token;
          return;
        }
        var start = ref.selectionStart || 0;
        var end = ref.selectionEnd || 0;
        this.message = this.message.slice(0, start) + token + this.message.slice(end);
        this.$nextTick(function () {
          ref.focus();
          ref.selectionStart = ref.selectionEnd = start + token.length;
        });
      },

      sampleVariables: function () {
        if (this.recipientMethod === "csv" && this.csv.rows.length) {
          var row = this.csv.rows[0];
          var vars = {};
          (this.csv.variables || []).forEach(function (k) {
            vars[k] = row[k];
          });
          return vars;
        }
        return {};
      },

      sendTest: function () {
        var self = this;
        this.testing = true;
        fetch(this.cfg.sendTestUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": this.cfg.csrfToken,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: JSON.stringify({
            from_email: this.fromEmail,
            template_id: this.mode === "template" ? this.templateId : null,
            subject: this.subject,
            text_body: this.message,
            html_body: "",
            variables: this.sampleVariables(),
          }),
        })
          .then(function (res) {
            return res.json().then(function (data) {
              return { ok: res.ok, data: data };
            });
          })
          .then(function (r) {
            if (window.toast) {
              window.toast(
                r.ok ? "success" : "danger",
                r.ok
                  ? "Test email sent — check your inbox."
                  : (r.data && r.data.error) || "Couldn't send the test email."
              );
            }
          })
          .catch(function () {
            if (window.toast) window.toast("danger", "Couldn't send the test email.");
          })
          .finally(function () {
            self.testing = false;
          });
      },

      onSubmit: function (event) {
        if (this._confirmed) return; // second pass — let it through
        event.preventDefault();
        var n = this.recipientCount;
        var who = n + " " + (n === 1 ? "person" : "people");
        var self = this;
        var run = function (ok) {
          if (!ok) return;
          self._confirmed = true;
          self.submitting = true;
          event.target.submit();
        };
        var store = window.Alpine && Alpine.store("confirm");
        if (!store) {
          // Modal not up yet: send nothing, and say so.
          if (window.toast) window.toast("danger", "Still loading. Try again in a moment.");
          return;
        }
        store
          .ask({
            title: "Send this campaign?",
            message: "This emails " + who + " now and can't be undone.",
            confirmLabel: "Send now",
            danger: true,
          })
          .then(run);
      },
    };
  }

  // Exposed as a global factory so `x-data="campaignWizard()"` resolves no
  // matter how this script and Alpine's own (deferred) script interleave.
  window.campaignWizard = campaignWizard;
})();
