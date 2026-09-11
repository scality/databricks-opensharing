// Setup page client. No framework, no CDN, no build step: this file is served by
// the same stdlib Python server that answers /api/*.
const $ = (id) => document.getElementById(id);

// The plain text/select fields, each one both an id in index.html and a key in
// the cfg dict the API takes. Anything structured (endpoint_mode, tables) is
// read and written by its own function below.
const FIELDS = ["platform", "s3_endpoint", "bucket", "access_key", "secret_key",
                "region", "share_public_url"];
const MODES = [["trusted", "mode_trusted"], ["private_ca", "mode_private_ca"],
               ["http", "mode_http"]];

// A Check click and a status refresh() render two DIFFERENT things — this run's
// prechecks, and the last applied verdict — and one must not erase the other.
// refresh() falls back to whatever prechecks are still current instead of
// repainting the box from s.verdict, which is null until the first Apply and so
// cleared a Check result the instant it appeared.
let lastPrechecks = null;
let lastWarnings = null;

// "Never logged in" (no session established yet on this page load) and "session
// expired" (one was, and a later call was refused) are two different nothings
// and must not share one sentence.
let everAuthenticated = false;

// One entry per row of the tables list, holding the elements it is made of.
// `painted` records what the server last put in each text field, so a later
// refresh can fill a field the operator has not touched without clobbering one
// they have.
let tableRows = [];

function currentMode() {
  for (const [value, id] of MODES) if ($(id).checked) return value;
  return "trusted";
}

function setMode(value) {
  for (const [v, id] of MODES) $(id).checked = (v === value);
  updateMode();
}

// The CA panel and the plain-HTTP notice belong to exactly one mode each, and
// the notice is persistent: an operator who picked plain HTTP keeps seeing what
// it costs, rather than a toast that has already gone.
function updateMode() {
  const mode = currentMode();
  $("ca_panel").hidden = mode !== "private_ca";
  $("http_panel").hidden = mode !== "http";
}

function addTableRow(t, checked) {
  const tr = document.createElement("tr");
  const cells = [];
  for (let i = 0; i < 5; i++) {
    const td = document.createElement("td");
    tr.appendChild(td);
    cells.push(td);
  }
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = !!checked;
  cells[0].appendChild(box);
  cells[1].textContent = t.prefix;
  const fields = {};
  for (const [i, name] of [[2, "share"], [3, "schema"], [4, "table"]]) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = t[name] || "";
    cells[i].appendChild(input);
    fields[name] = input;
  }
  const row = {
    prefix: t.prefix,
    box,
    share: fields.share,
    schema: fields.schema,
    table: fields.table,
    painted: {share: fields.share.value, schema: fields.schema.value,
              table: fields.table.value},
  };
  tableRows.push(row);
  $("tables_body").appendChild(tr);
  return row;
}

function findRow(prefix) {
  for (const row of tableRows) if (row.prefix === prefix) return row;
  return null;
}

// Merge, never replace. A browse or a status repaint must not throw away a row
// the operator has already selected or renamed: an existing prefix keeps its
// checkbox, and each of its three names is refilled only when it is empty or
// still holds exactly what the server last put there.
function mergeTables(incoming, checkedWhenNew) {
  for (const t of incoming || []) {
    const row = findRow(t.prefix);
    if (!row) {
      addTableRow(t, checkedWhenNew);
      continue;
    }
    for (const name of ["share", "schema", "table"]) {
      const value = t[name] || "";
      const field = row[name];
      if (field.value === "" || field.value === row.painted[name]) {
        field.value = value;
        row.painted[name] = value;
      }
    }
  }
}

// The secret is write-only. It is never returned by /api/status, so it is never
// written back into the field — an empty box means "unchanged", not "unset",
// and is sent as "" for the server to read that way. ca_pem_sha256 is owned by
// the CA upload and is deliberately not sent from here.
function readConfig() {
  const cfg = {};
  for (const f of FIELDS) cfg[f] = $(f).value;
  cfg.endpoint_mode = currentMode();
  cfg.tables = [];
  for (const row of tableRows) {
    if (!row.box.checked) continue;
    cfg.tables.push({
      prefix: row.prefix,
      share: row.share.value,
      schema: row.schema.value,
      table: row.table.value,
    });
  }
  return cfg;
}

function renderLines(boxId, lines, className) {
  const box = $(boxId);
  box.innerHTML = "";
  for (const line of lines || []) {
    const p = document.createElement("p");
    p.className = className;
    p.textContent = line;
    box.appendChild(p);
  }
}

function renderChecks(list) {
  const box = $("checks");
  box.innerHTML = "";
  for (const c of list || []) {
    const p = document.createElement("p");
    p.className = c.result;              // pass | fail | unknown
    p.textContent = `${c.result.toUpperCase()} — ${c.detail || c.id}`;
    box.appendChild(p);
  }
}

function showLogin(message) {
  $("login_error").hidden = !message;
  if (message) $("login_error").textContent = message;
  $("login").hidden = false;
  $("app").hidden = true;
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
}

// The apply/rotate/verify diagnostic lives in its own element. Appended to
// #state's textContent it was erased by the next status poll, which overwrites
// that element wholesale.
function setStateDetail(text) {
  $("state_detail").textContent = text || "";
  $("state_detail").hidden = !text;
}

async function refresh() {
  const r = await fetch("/api/status");
  if (r.status === 401) {
    showLogin(everAuthenticated ? "Session expired — log in again." : "");
    return;
  }
  everAuthenticated = true;
  showApp();
  const s = await r.json();
  $("state").textContent = s.copy || s.state;
  // Handover is gated on the server's verdict, not on anything this script decides.
  $("profile").disabled = s.state !== "verified";
  $("token_expires").textContent = s.token_expires || "not set";
  if (s.ca) {
    $("ca_sha256").textContent = s.ca.present ? (s.ca.sha256 || "present") : "none";
  }
  if (s.config) {
    for (const f of FIELDS) {
      if (f !== "secret_key" && s.config[f]) $(f).value = s.config[f];
    }
    if (s.config.endpoint_mode) setMode(s.config.endpoint_mode);
    mergeTables(s.config.tables, true);
  }
  // `[]` is truthy, so `verdict.checks && []` would erase whatever prechecks are
  // still current the moment a verdict exists with zero checks in it. An explicit
  // length check treats that the same as no verdict rather than as "nothing to show".
  const verdictChecks = s.verdict && s.verdict.checks;
  renderChecks((verdictChecks && verdictChecks.length) ? verdictChecks : lastPrechecks);
  const warnings = (s.warnings && s.warnings.length) ? s.warnings : lastWarnings;
  renderLines("warnings", warnings, "warn");
}

// Enter in a single text field submits its enclosing <form> natively, which
// requires the field to be IN a form. The submit handler covers both the Enter
// path and a click on the type="submit" button — a browser dispatches "submit"
// for either, so there is one handler rather than two that could disagree.
$("login_form").onsubmit = async (e) => {
  e.preventDefault();
  const r = await fetch("/api/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({token: $("login_token").value}),
  });
  if (r.status !== 204) {
    showLogin("Wrong token.");
    return;
  }
  $("login_token").value = "";
  await refresh();
};

for (const [, id] of MODES) $(id).onchange = updateMode;

$("ca_file").onchange = () => {
  const input = $("ca_file");
  const file = input.files && input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { $("ca_pem").value = String(reader.result || ""); };
  reader.readAsText(file);
};

$("ca_upload").onclick = async () => {
  $("ca_error").hidden = true;
  const r = await fetch("/api/ca", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pem: $("ca_pem").value}),
  });
  if (r.status === 401) return refresh();
  const out = await r.json();
  if (!r.ok || out.error) {
    $("ca_error").hidden = false;
    $("ca_error").textContent = out.error || "The certificate was refused.";
    return;
  }
  $("ca_pem").value = "";
  await refresh();
};

$("ca_remove").onclick = async () => {
  const r = await fetch("/api/ca", {method: "DELETE"});
  if (r.status === 401) return refresh();
  await refresh();
};

$("browse").onclick = async () => {
  $("browse_error").hidden = true;
  const r = await fetch("/api/browse?prefix=");
  if (r.status === 401) return refresh();
  const out = await r.json();
  if (!r.ok || out.error) {
    $("browse_error").hidden = false;
    $("browse_error").textContent = out.error || "The bucket could not be listed.";
    return;
  }
  mergeTables(out.tables, true);
  $("tables_truncated").hidden = !out.truncated;
};

$("add_prefix").onclick = () => {
  const prefix = prompt("Prefix of the Delta table, without a leading slash:");
  if (!prefix) return;
  if (findRow(prefix)) return;
  addTableRow({prefix, share: "", schema: "", table: ""}, true);
};

$("check").onclick = async () => {
  const r = await fetch("/api/config", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(readConfig()),
  });
  if (r.status === 401) return refresh();
  const out = await r.json();
  // Problems are field names, never values — the server never echoes a secret.
  renderLines("problems", out.problems, "fail");
  lastWarnings = out.warnings || [];
  lastPrechecks = (out.problems || []).map((p) => ({result: "fail", detail: p}))
    .concat(out.prechecks || []);
  await refresh();
};

$("apply").onclick = async () => {
  setStateDetail("");
  $("state").textContent = "Applying and verifying…";
  const r = await fetch("/api/apply", {method: "POST"});
  if (r.status === 401) return refresh();
  const out = await r.json();
  await refresh();
  // The supervisor's own diagnostic — render stderr, the log tail, the recovery
  // outcome, or the 409 that says another operation is already running. The
  // server redacts any live secret out of it before it is sent.
  if (out.detail) setStateDetail(out.detail);
};

$("verify").onclick = async () => {
  setStateDetail("");
  const r = await fetch("/api/verify", {method: "POST"});
  if (r.status === 401) return refresh();
  const out = await r.json();
  await refresh();
  if (out.detail) setStateDetail(out.detail);
};

$("rotate").onclick = async () => {
  if (!confirm("Rotate the bearer token? Every profile already handed out stops working.")) return;
  setStateDetail("");
  const r = await fetch("/api/token/rotate", {method: "POST"});
  if (r.status === 401) return refresh();
  const out = await r.json();
  await refresh();
  if (out.detail) setStateDetail(out.detail);
};

$("profile").onclick = async () => {
  const r = await fetch("/api/profile");
  if (r.status === 401) return refresh();
  if (!r.ok) { alert("Not verified — run Verify first."); return; }
  const blob = new Blob([await r.text()], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "scality.share";
  a.click();
};

// While an apply, verify or rotate is in flight the server can take several
// seconds (render, restart, grace, gates). This keeps the visible state current
// instead of freezing on "Applying and verifying…" until the fetch resolves.
let pollTimer = null;
function pollWhileBusy(promise) {
  if (pollTimer) return promise;
  pollTimer = setInterval(refresh, 1000);
  return promise.finally(() => { clearInterval(pollTimer); pollTimer = null; });
}
const _origApplyClick = $("apply").onclick;
$("apply").onclick = () => pollWhileBusy(_origApplyClick());
const _origVerifyClick = $("verify").onclick;
$("verify").onclick = () => pollWhileBusy(_origVerifyClick());
const _origRotateClick = $("rotate").onclick;
$("rotate").onclick = () => pollWhileBusy(_origRotateClick());

updateMode();
refresh();
