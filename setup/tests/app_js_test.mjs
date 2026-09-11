#!/usr/bin/env node
// Behavioural tests for the setup page's client script (setup/static/app.js),
// run with plain Node and `vm` — no npm, no jsdom. The DOM shim below is
// derived from the shipped markup rather than hand-listed, so renaming an id in
// index.html changes what this harness serves exactly as it changes what a
// browser serves, and a structural test separately asserts that every id the
// script reaches for is one the markup actually declares.
//
// Run: node setup/tests/app_js_test.mjs
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.join(HERE, "..", "static");
const APP_JS = fs.readFileSync(path.join(STATIC_DIR, "app.js"), "utf8");
const INDEX_HTML = fs.readFileSync(path.join(STATIC_DIR, "index.html"), "utf8");

function idsInHtml(html) {
  const ids = new Set();
  const re = /\bid="([A-Za-z_][\w-]*)"/g;
  let m;
  while ((m = re.exec(html))) ids.add(m[1]);
  return ids;
}

// Every id app.js asks the DOM for: literal `$("...")` calls, plus the string
// literals in the two arrays its dynamic `$(f)` calls are drawn from (FIELDS,
// MODES). Anything reached neither way is outside what a structural check can
// see; the runtime harness still throws the moment such a lookup executes.
function idsReferencedByScript(js) {
  const ids = new Set();
  const literal = /\$\("([A-Za-z_][\w-]*)"\)/g;
  let m;
  while ((m = literal.exec(js))) ids.add(m[1]);
  for (const name of ["FIELDS", "MODES"]) {
    const decl = js.match(new RegExp("const " + name + " = \\[([\\s\\S]*?)\\];"));
    if (!decl) continue;
    const item = /"([A-Za-z_][\w-]*)"/g;
    let fm;
    while ((fm = item.exec(decl[1]))) ids.add(fm[1]);
  }
  // MODES pairs a cfg value with an element id; "trusted" / "private_ca" /
  // "http" are values, not ids, and the markup does not declare them.
  for (const value of ["trusted", "private_ca", "http"]) ids.delete(value);
  return ids;
}

const HTML_IDS = idsInHtml(INDEX_HTML);
const SCRIPT_IDS = idsReferencedByScript(APP_JS);

function makeElement(tag) {
  const children = [];
  return {
    tagName: tag || "div",
    _value: "",
    get value() { return this._value; },
    set value(v) { this._value = String(v); },
    textContent: "", className: "", hidden: false, disabled: false,
    type: "", checked: false, files: null, accept: "", placeholder: "",
    href: "", download: "", onclick: null, onsubmit: null, onchange: null,
    children,
    get innerHTML() { return children.length ? "x" : ""; },
    set innerHTML(v) { if (v === "") children.length = 0; },
    appendChild(c) { children.push(c); return c; },
    click() {},
  };
}

function jsonResponse(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function newPage(responder, options) {
  const registry = {};
  for (const id of HTML_IDS) registry[id] = makeElement();
  const calls = [];
  const document = {
    getElementById: (id) => {
      if (!(id in registry)) throw new Error("no such element: " + id);
      return registry[id];
    },
    createElement: (tag) => makeElement(tag),
  };
  const fetchFn = async (url, opts) => {
    calls.push({ url, opts: opts || {} });
    return responder(url, opts || {});
  };
  const context = {
    document,
    fetch: fetchFn,
    console,
    confirm: () => true,
    alert: () => {},
    prompt: () => (options && options.prompt) || "",
    Blob: function (parts) { this.parts = parts; },
    URL: { createObjectURL: () => "blob:fake" },
    setInterval: () => 0,
    clearInterval: () => {},
  };
  vm.createContext(context);
  new vm.Script(APP_JS, { filename: "app.js" }).runInContext(context);
  return { registry, calls, context };
}

// Drains the microtask queue enough for chained async handlers
// (onclick -> fetch -> refresh -> fetch -> ...) to settle before asserting.
async function settle(n = 8) {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
}

function fakeSubmitEvent() {
  return { preventDefault() {} };
}

// The rows the page built, read back the way readConfig() sees them: one entry
// per row of #tables_body, in order.
function rowValues(registry) {
  return registry.tables_body.children.map((tr) => {
    const cells = tr.children;
    return {
      checked: cells[0].children[0].checked,
      prefix: cells[1].textContent,
      share: cells[2].children[0].value,
      schema: cells[3].children[0].value,
      table: cells[4].children[0].value,
    };
  });
}

const CONFIG = {
  platform: "ring",
  endpoint_mode: "trusted",
  s3_endpoint: "https://s3.example.com",
  bucket: "delta-share",
  access_key: "AKEXAMPLE",
  region: "us-east-1",
  share_public_url: "https://share.example.com",
  tables: [{ prefix: "poc/customers", share: "scality", schema: "poc", table: "customers" }],
};

function statusBody(extra) {
  return Object.assign({
    state: "never_verified",
    copy: "Configured, never verified.",
    config: CONFIG,
    verdict: null,
    prechecks: [],
    warnings: [],
    ca: { present: false, sha256: "" },
    token_expires: "",
  }, extra || {});
}

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

test("every id app.js references exists in the shipped index.html", () => {
  const missing = [...SCRIPT_IDS].filter((id) => !HTML_IDS.has(id));
  assert.deepEqual(missing, [],
    "app.js references ids missing from index.html: " + missing.join(", "));
});

test("no session on load shows the login form, not the setup form", async () => {
  const { registry } = newPage(async (url) => {
    if (url === "/api/status") return jsonResponse(401, {});
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  assert.equal(registry.login.hidden, false);
  assert.equal(registry.app.hidden, true);
});

test("submitting the login form (the Enter path) POSTs /api/login and reveals the page",
  async () => {
    let loggedIn = false;
    const { registry, calls } = newPage(async (url) => {
      if (url === "/api/status") {
        return loggedIn ? jsonResponse(200, statusBody()) : jsonResponse(401, {});
      }
      if (url === "/api/login") {
        loggedIn = true;
        return { status: 204, ok: true, json: async () => ({}), text: async () => "" };
      }
      throw new Error("unexpected fetch " + url);
    });
    await settle();
    assert.ok(registry.login_form, "the token field must live inside a <form>");
    assert.equal(typeof registry.login_form.onsubmit, "function",
      "the form must have a submit handler — otherwise Enter has nowhere to go");
    registry.login_token.value = "the-bootstrap-token";
    await registry.login_form.onsubmit(fakeSubmitEvent());
    await settle();
    const loginCall = calls.find((c) => c.url === "/api/login");
    assert.equal(loginCall.opts.method, "POST");
    assert.equal(JSON.parse(loginCall.opts.body).token, "the-bootstrap-token");
    assert.equal(registry.login.hidden, true);
    assert.equal(registry.app.hidden, false);
  });

test("never-logged-in and session-expired render different sentences", async () => {
  const { registry: never } = newPage(async (url) => {
    if (url === "/api/status") return jsonResponse(401, {});
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  const neverMessage = never.login_error.hidden ? "" : never.login_error.textContent;

  let call = 0;
  const { registry: expired, context } = newPage(async (url) => {
    if (url === "/api/status") {
      call += 1;
      return call === 1 ? jsonResponse(200, statusBody()) : jsonResponse(401, {});
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  assert.equal(expired.app.hidden, false, "sanity: first load authenticated");
  await context.refresh();
  await settle();
  const expiredMessage = expired.login_error.hidden ? "" : expired.login_error.textContent;
  assert.notEqual(expiredMessage, "", "an expired session must say something");
  assert.notEqual(neverMessage, expiredMessage,
    "never-logged-in and session-expired must not share one sentence");
});

test("the mode radios toggle the CA panel and the plain-HTTP notice", async () => {
  const { registry } = newPage(async (url) => {
    if (url === "/api/status") return jsonResponse(200, statusBody());
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  assert.equal(registry.ca_panel.hidden, true, "trusted hides the CA panel");
  assert.equal(registry.http_panel.hidden, true, "trusted hides the HTTP notice");

  registry.mode_trusted.checked = false;
  registry.mode_private_ca.checked = true;
  registry.mode_private_ca.onchange();
  assert.equal(registry.ca_panel.hidden, false, "private CA reveals the CA panel");
  assert.equal(registry.http_panel.hidden, true);

  registry.mode_private_ca.checked = false;
  registry.mode_http.checked = true;
  registry.mode_http.onchange();
  assert.equal(registry.ca_panel.hidden, true);
  assert.equal(registry.http_panel.hidden, false, "plain HTTP reveals the notice");
});

test("readConfig sends only the checked tables, omits an empty secret as \"\", " +
     "and never sends ca_pem_sha256", async () => {
  const { registry, context } = newPage(async (url) => {
    if (url === "/api/status") {
      return jsonResponse(200, statusBody({
        config: Object.assign({}, CONFIG, {
          tables: [
            { prefix: "poc/customers", share: "scality", schema: "poc", table: "customers" },
            { prefix: "poc/orders", share: "scality", schema: "poc", table: "orders" },
          ],
        }),
        ca: { present: true, sha256: "abc123" },
      }));
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  assert.equal(rowValues(registry).length, 2, "both configured prefixes became rows");

  // The operator unchecks the second row; the secret field is left alone.
  registry.tables_body.children[1].children[0].children[0].checked = false;
  assert.equal(registry.secret_key.value, "", "the secret is never painted back");

  const cfg = context.readConfig();
  // Compared as JSON: objects built inside the vm realm have their own Object
  // prototype, which assert/strict's deepEqual counts as a difference.
  assert.equal(JSON.stringify(cfg.tables), JSON.stringify([
    { prefix: "poc/customers", share: "scality", schema: "poc", table: "customers" },
  ]), "only the checked row is sent");
  assert.equal(cfg.secret_key, "", "an untouched secret field is sent as the empty string");
  assert.equal("ca_pem_sha256" in cfg, false, "the CA hash is owned by the upload route");
  assert.equal(cfg.endpoint_mode, "trusted");
  assert.equal(cfg.share_public_url, "https://share.example.com");
});

test("browse merges into the table and does not clobber a row the operator edited",
  async () => {
    const { registry } = newPage(async (url) => {
      if (url === "/api/status") return jsonResponse(200, statusBody());
      if (url.startsWith("/api/browse")) {
        return jsonResponse(200, {
          tables: [
            // Same prefix, different names than the operator now has on screen.
            { prefix: "poc/customers", share: "delta_share", schema: "poc", table: "customers" },
            { prefix: "poc/orders", share: "delta_share", schema: "poc", table: "orders" },
          ],
          truncated: true,
        });
      }
      throw new Error("unexpected fetch " + url);
    });
    await settle();
    assert.equal(rowValues(registry).length, 1);

    // The operator renames the share on the row already on screen.
    registry.tables_body.children[0].children[2].children[0].value = "my_share";

    await registry.browse.onclick();
    await settle();

    const rows = rowValues(registry);
    assert.equal(rows.length, 2, "the newly discovered prefix was added");
    assert.equal(rows[0].prefix, "poc/customers");
    assert.equal(rows[0].share, "my_share", "browse must not overwrite an edited field");
    assert.equal(rows[0].schema, "poc", "an unedited field may be refreshed");
    assert.equal(rows[1].prefix, "poc/orders");
    assert.equal(rows[1].share, "delta_share");
    assert.equal(registry.tables_truncated.hidden, false,
      "a truncated listing must say so");
  });

test("a browse the storage refuses shows the error and adds no rows", async () => {
  const { registry } = newPage(async (url) => {
    if (url === "/api/status") return jsonResponse(200, statusBody());
    if (url.startsWith("/api/browse")) {
      return jsonResponse(502, { error: "credentials refused" });
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  await registry.browse.onclick();
  await settle();
  assert.equal(registry.browse_error.hidden, false);
  assert.ok(registry.browse_error.textContent.includes("credentials refused"));
  assert.equal(rowValues(registry).length, 1, "no phantom rows on an error");
});

test("Check renders problems and prechecks, and the trailing refresh does not erase them",
  async () => {
    const { registry } = newPage(async (url, opts) => {
      if (url === "/api/status") return jsonResponse(200, statusBody({ config: null }));
      if (url === "/api/config" && opts.method === "PUT") {
        return jsonResponse(200, {
          ok: false,
          problems: ["secret_key is required"],
          warnings: ["No public share URL: the .share file will point at this host's own address."],
          prechecks: [{ id: "endpoint_reachable", result: "pass", detail: "HTTP 403" }],
        });
      }
      throw new Error("unexpected fetch " + url);
    });
    await settle();
    await registry.check.onclick();
    await settle();
    assert.equal(registry.problems.children.length, 1);
    assert.equal(registry.problems.children[0].textContent, "secret_key is required");
    assert.equal(registry.warnings.children.length, 1);
    assert.equal(registry.checks.children.length, 2,
      "the Check result must still be on screen after the trailing refresh()");
    assert.equal(registry.checks.children[0].textContent, "FAIL — secret_key is required");
    assert.equal(registry.checks.children[1].textContent, "PASS — HTTP 403");
  });

test("a status repaint with an empty verdict.checks keeps the last prechecks", async () => {
  // `[]` is truthy, so `(s.verdict && s.verdict.checks) || lastPrechecks` would
  // render nothing the moment a verdict exists with zero checks in it.
  let applied = false;
  const { registry } = newPage(async (url, opts) => {
    if (url === "/api/status") {
      return jsonResponse(200, statusBody(applied
        ? { verdict: { hash: "h", checks: [] } }
        : { verdict: null }));
    }
    if (url === "/api/config" && opts.method === "PUT") {
      return jsonResponse(200, { ok: false, problems: ["secret_key is required"], prechecks: [] });
    }
    if (url === "/api/apply") {
      applied = true;
      return jsonResponse(200, { ok: true, detail: "server started" });
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  await registry.check.onclick();
  await settle();
  assert.equal(registry.checks.children.length, 1, "precheck result visible");

  await registry.apply.onclick();
  await settle();
  assert.equal(registry.checks.children.length, 1,
    "an empty verdict.checks must not erase the last prechecks");
  assert.equal(registry.checks.children[0].textContent, "FAIL — secret_key is required");
});

test("a real verdict supersedes stale prechecks", async () => {
  let applied = false;
  const { registry } = newPage(async (url, opts) => {
    if (url === "/api/status") {
      return jsonResponse(200, statusBody(applied
        ? { state: "verified", verdict: { hash: "h", checks: [{ id: "suite_completed", result: "pass" }] } }
        : { verdict: null }));
    }
    if (url === "/api/config" && opts.method === "PUT") {
      return jsonResponse(200, { ok: false, problems: ["secret_key is required"], prechecks: [] });
    }
    if (url === "/api/apply") {
      applied = true;
      return jsonResponse(200, { ok: true });
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  await registry.check.onclick();
  await settle();
  await registry.apply.onclick();
  await settle();
  assert.equal(registry.checks.children.length, 1);
  assert.equal(registry.checks.children[0].textContent, "PASS — suite_completed");
  assert.equal(registry.profile.disabled, false, "verified enables the handover download");
});

test("an apply refused as busy (409) shows the server's detail", async () => {
  const { registry, context } = newPage(async (url) => {
    if (url === "/api/status") return jsonResponse(200, statusBody());
    if (url === "/api/apply") {
      return jsonResponse(409, { ok: false, busy: true,
        detail: "Another operation is already running." });
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  await registry.apply.onclick();
  await settle();
  assert.equal(registry.state_detail.hidden, false);
  assert.ok(registry.state_detail.textContent.includes("Another operation is already running."),
    "a 409 carries a detail and the page must show it");

  // A later status poll — what pollWhileBusy's interval triggers — must not
  // erase it: the detail has its own element, not the one refresh() overwrites.
  await context.refresh();
  await settle();
  assert.ok(registry.state_detail.textContent.includes("Another operation is already running."));
});

test("the .share download is disabled until the server says verified", async () => {
  const { registry } = newPage(async (url) => {
    if (url === "/api/status") return jsonResponse(200, statusBody({ state: "degraded" }));
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  assert.equal(registry.profile.disabled, true);
});

test("uploading a CA reports a refusal and a success", async () => {
  let stored = null;
  const { registry } = newPage(async (url, opts) => {
    if (url === "/api/status") {
      return jsonResponse(200, statusBody({
        ca: stored ? { present: true, sha256: stored } : { present: false, sha256: "" },
      }));
    }
    if (url === "/api/ca" && opts.method === "PUT") {
      const pem = JSON.parse(opts.body).pem;
      if (!pem.includes("BEGIN CERTIFICATE")) {
        return jsonResponse(400, { error: "not a PEM certificate" });
      }
      stored = "deadbeef";
      return jsonResponse(200, { ok: true, sha256: stored });
    }
    throw new Error("unexpected fetch " + url);
  });
  await settle();
  registry.ca_pem.value = "garbage";
  await registry.ca_upload.onclick();
  await settle();
  assert.equal(registry.ca_error.hidden, false);
  assert.ok(registry.ca_error.textContent.includes("not a PEM certificate"));

  registry.ca_pem.value = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----";
  await registry.ca_upload.onclick();
  await settle();
  assert.equal(registry.ca_error.hidden, true);
  assert.equal(registry.ca_pem.value, "", "the textarea is cleared once the CA is stored");
  assert.equal(registry.ca_sha256.textContent, "deadbeef");
});

let failed = 0;
for (const { name, fn } of tests) {
  try {
    await fn();
    console.log("  ok   " + name);
  } catch (e) {
    failed += 1;
    console.log("  FAIL " + name);
    console.log("     " + (e && e.stack ? e.stack : String(e)));
  }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
