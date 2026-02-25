import * as monaco from "monaco-editor";
import Ajv from "ajv/dist/2020";

const style = document.createElement("style");
style.textContent = `
  header{padding:12px 16px;border-bottom:1px solid #ddd;display:flex;gap:12px;align-items:center}
  main{display:grid;grid-template-columns:1fr 420px;height:calc(100vh - 50px)}
  #editor{height:100%}
  #side{border-left:1px solid #ddd;padding:12px;overflow:auto}
  button{padding:8px 10px}
  pre{white-space:pre-wrap;word-break:break-word;background:#f7f7f7;padding:10px;border-radius:8px}
  .row{display:flex;gap:8px;flex-wrap:wrap}
  input{padding:7px 8px;width:100%}
  .small{font-size:12px;color:#666}
`;
document.head.appendChild(style);

document.getElementById("app").innerHTML = `
  <header>
    <strong>Roster DSL Editor</strong>
    <span class="small">Offline bundle (no CDN)</span>
    <div style="flex:1"></div>
    <div class="row">
      <button id="btnSchema">Validate (Schema)</button>
      <button id="btnApiValidate">Validate (API)</button>
      <button id="btnSubmit">Submit Job</button>
    </div>
  </header>
  <main>
    <div id="editor"></div>
    <div id="side">
      <div class="small">Job ID</div>
      <input id="jobId" placeholder="incolla job_id qui" />
      <div class="row" style="margin-top:8px;">
        <button id="btnStatus">Get Status</button>
        <button id="btnResult">Get Result</button>
      </div>
      <h3>Output</h3>
      <pre id="out">Pronto.</pre>
    </div>
  </main>
`;

const out = (obj) => {
  document.getElementById("out").textContent =
    typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
};

async function fetchJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
  return await r.json();
}

async function apiPost(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) throw { status: r.status, data };
  return data;
}

async function apiGet(path) {
  const r = await fetch(path);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) throw { status: r.status, data };
  return data;
}

(async () => {
  const schema = await fetchJSON("/dsl.schema.json");
  const spec = await fetchJSON("/spec.json");

  const ajv = new Ajv({ allErrors: true, strict: false });
  const validateFn = ajv.compile(schema);

  const editor = monaco.editor.create(document.getElementById("editor"), {
    value: JSON.stringify(spec, null, 2),
    language: "json",
    automaticLayout: true,
    minimap: { enabled: false }
  });

  document.getElementById("btnSchema").onclick = () => {
    try {
      const obj = JSON.parse(editor.getValue());
      const ok = validateFn(obj);
      out(ok ? { ok: true, mode: "schema" } : { ok: false, mode: "schema", errors: validateFn.errors });
    } catch (e) {
      out({ ok: false, mode: "schema", error: String(e) });
    }
  };

  document.getElementById("btnApiValidate").onclick = async () => {
    try {
      const specObj = JSON.parse(editor.getValue());
      out(await apiPost("/api/validate", { spec: specObj }));
    } catch (e) { out(e); }
  };

  document.getElementById("btnSubmit").onclick = async () => {
    try {
      const specObj = JSON.parse(editor.getValue());
      const res = await apiPost("/api/jobs", { spec: specObj, max_time_seconds: 60, workers: 8 });
      document.getElementById("jobId").value = res.job_id || "";
      out(res);
    } catch (e) { out(e); }
  };

  document.getElementById("btnStatus").onclick = async () => {
    try {
      const jobId = document.getElementById("jobId").value.trim();
      if (!jobId) return out("Inserisci job_id.");
      out(await apiGet(`/api/jobs/${jobId}`));
    } catch (e) { out(e); }
  };

  document.getElementById("btnResult").onclick = async () => {
    try {
      const jobId = document.getElementById("jobId").value.trim();
      if (!jobId) return out("Inserisci job_id.");
      out(await apiGet(`/api/jobs/${jobId}/result`));
    } catch (e) { out(e); }
  };

  out("Pronto. (Offline) Modifica il JSON e prova Validate/Jobs.");
})().catch(err => out({ error: String(err) }));
