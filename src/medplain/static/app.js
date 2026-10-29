"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = { packets: [], selected: null, detail: null, busy: false, loading: true, loadIssue: "", selection: 0, result: null };
  const agentLabels = {
    LabAndDischargeParserAgent: "Lab & discharge parser",
    DrugAndContraindicationVerifierAgent: "Medication verifier",
    PatientAdvocateExplainerAgent: "Patient advocate",
    AtroposSafetyGate: "Atropos safety gate",
    AtroposClinicalSafetyGate: "Atropos safety gate",
    Atropos: "Atropos safety gate",
    AdversarialStressInjector: "Deliberate dosage stress test",
    StrandsGraph: "Strands correction handoff",
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function show(id, visible) { byId(id).hidden = !visible; }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 120000);
    try {
      const isRead = String(options.method || "GET").toUpperCase() === "GET";
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const response = await fetch(path, { ...options, signal: controller.signal, headers: { Accept: "application/json", ...(options.headers || {}) } });
        if (response.status === 429) {
          if (isRead && attempt < 2) {
            if (path.startsWith("/api/packets") && state.loading) {
              state.loadIssue = "The demo is busy. Retrying the packet shortly; the button becomes available when it loads.";
              updateControls();
            }
            await new Promise((resolve) => window.setTimeout(resolve, attempt === 0 ? 500 : 1500));
            continue;
          }
          throw new Error("The demo is busy right now. Wait a moment and try again.");
        }
        if (response.status === 503) throw new Error("The demo is temporarily unavailable. Please try again shortly.");
        if (response.status >= 500) throw new Error("The demo could not finish this request. Please try again shortly.");
        let payload;
        try { payload = await response.json(); } catch { throw new Error("The service returned an unreadable response. Please try again."); }
        if (!response.ok) {
          const detail = typeof payload.detail === "string" ? payload.detail : "";
          throw new Error(detail || `The service could not complete this request (HTTP ${response.status}). Please try again.`);
        }
        return payload;
      }
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. The explanation has not been published. Please try again.");
      if (error instanceof TypeError) throw new Error("The service could not be reached. Check your connection and try again.");
      throw error;
    } finally { window.clearTimeout(timeout); }
  }

  function updateControls() {
    const disabled = state.busy || state.loading || !state.detail;
    byId("explain-button").disabled = disabled;
    byId("explain-button-text").textContent = state.busy ? "Reviewing the packet..." : "Explain this packet";
    byId("button-reason").textContent = state.busy
      ? "The agents are running. This button becomes available when the review finishes."
      : state.loading
        ? state.loadIssue || "Loading synthetic packets. The button becomes available when a packet is ready."
        : !state.detail
          ? state.loadIssue ? `${state.loadIssue} Use the loading retry above.` : "A packet is not available yet. Use the loading retry above."
          : "Uses only the synthetic packet shown. No patient upload.";
    byId("stress-toggle").disabled = state.busy;
    if (state.busy) {
      byId("stress-note").textContent = "The stress-test setting is locked while this review is running.";
      show("stress-note", true);
    } else {
      byId("stress-note").textContent = "This packet includes an adversarial dosage stress test.";
      show("stress-note", Boolean(state.detail?.adversarial));
    }
    document.querySelectorAll(".packet-card").forEach((card) => { card.disabled = state.busy; });
    show("packet-lock-reason", state.busy);
  }

  function resetResults() {
    state.result = null;
    show("explanation-empty", true);
    show("explanation", false);
    show("run-error", false);
    show("run-progress", false);
    show("verification-section", false);
    byId("result-badge").textContent = "AWAITING CHECK";
    delete byId("result-badge").dataset.status;
    byId("download-receipt").disabled = true;
    byId("download-reason").textContent = "A receipt becomes available after a verification run.";
    byId("verify-receipt").disabled = true;
    byId("chain-verification").textContent = "Run a packet to check its receipt chain.";
  }

  function renderPacketButtons() {
    const container = byId("packet-options");
    container.replaceChildren();
    state.packets.forEach((packet, index) => {
      const card = element("button", "packet-card");
      card.type = "button";
      card.dataset.packetId = packet.packet_id;
      card.dataset.adversarial = String(Boolean(packet.adversarial));
      card.setAttribute("aria-pressed", String(state.selected === packet.packet_id));
      card.append(
        element("span", "packet-number", packet.adversarial ? "SAFETY STRESS TEST" : `EXAMPLE ${String(index + 1).padStart(2, "0")}`),
        element("span", "packet-name", packet.title),
        element("span", "packet-description", packet.subtitle || packet.patient || "Synthetic discharge & lab packet"),
        element("span", "packet-radio"),
      );
      card.lastElementChild.setAttribute("aria-hidden", "true");
      card.addEventListener("click", () => selectPacket(packet.packet_id));
      container.append(card);
    });
  }

  async function selectPacket(packetId) {
    if (state.busy) return;
    const selection = ++state.selection;
    state.selected = packetId;
    state.loading = true;
    state.loadIssue = "";
    state.detail = null;
    resetResults();
    renderPacketButtons();
    show("load-error", false);
    byId("source-text").textContent = "Loading the selected synthetic record...";
    byId("packet-title").textContent = state.packets.find((packet) => packet.packet_id === packetId)?.title || "Synthetic discharge record";
    byId("packet-subtitle").textContent = "Loading the source record.";
    show("fhir-details", false);
    updateControls();
    try {
      const packet = await api(`/api/packets/${encodeURIComponent(packetId)}`);
      if (selection !== state.selection) return;
      state.detail = packet;
      byId("packet-title").textContent = packet.title;
      byId("packet-subtitle").textContent = `${packet.patient || "Synthetic patient"} · ${packet.subtitle || "Hospital discharge record"}`;
      byId("source-text").textContent = packet.source_text || "The source record is not available.";
      byId("stress-toggle").checked = Boolean(packet.adversarial);
      if (packet.fhir && Object.keys(packet.fhir).length) {
        byId("fhir-json").textContent = JSON.stringify(packet.fhir, null, 2);
        show("fhir-details", true);
      }
    } catch (error) {
      if (selection !== state.selection) return;
      byId("load-error-text").textContent = error.message;
      state.loadIssue = error.message;
      show("load-error", true);
      byId("source-text").textContent = "This record could not be loaded. Use the retry above.";
      byId("packet-subtitle").textContent = "The selected source record could not be loaded.";
    } finally {
      if (selection === state.selection) { state.loading = false; updateControls(); }
    }
  }

  function setRuntime(mode) {
    const bedrock = mode === "bedrock";
    byId("runtime-mode").dataset.mode = bedrock ? "bedrock" : "fixture";
    byId("runtime-mode").textContent = bedrock ? "Strands SDK · Amazon Bedrock model" : `Strands SDK · ${mode === "fixture" || !mode ? "Offline fixture model" : `${mode} model`}`;
  }

  async function loadHealth() {
    try {
      const health = await api("/api/health");
      setRuntime(health.model_mode);
      if (health.commit && health.commit !== "unknown") byId("build-commit").textContent = `Build ${String(health.commit).slice(0, 12)} · Synthetic examples only`;
    } catch {
      byId("runtime-mode").textContent = "Runtime status unavailable. Packet requests may be retried.";
    }
  }

  async function loadPackets() {
    state.loading = true;
    state.loadIssue = "";
    show("load-error", false);
    updateControls();
    try {
      const payload = await api("/api/packets");
      state.packets = Array.isArray(payload) ? payload : payload.packets;
      if (!Array.isArray(state.packets) || !state.packets.length) throw new Error("No synthetic packets are available. Please try loading again.");
      renderPacketButtons();
      await selectPacket(state.selected && state.packets.some((packet) => packet.packet_id === state.selected) ? state.selected : state.packets[0].packet_id);
    } catch (error) {
      state.loading = false;
      byId("load-error-text").textContent = error.message;
      state.loadIssue = error.message;
      show("load-error", true);
      byId("packet-options").replaceChildren(element("p", "loading-copy", "Synthetic packets are temporarily unavailable."));
      updateControls();
    }
  }

  function appendList(parent, title, values, className = "explanation-list") {
    if (!Array.isArray(values) || !values.length) return;
    parent.append(element("h5", "", title));
    const list = element("ul", className);
    values.forEach((value) => list.append(element("li", "", typeof value === "string" ? value : JSON.stringify(value))));
    parent.append(list);
  }

  function renderExplanation(result) {
    const data = result.explanation;
    const container = byId("explanation");
    container.replaceChildren();
    const title = element("h4", "", "Here is what your packet says.");
    title.tabIndex = -1;
    container.append(title, element("p", "explanation-summary", data.summary || ""));
    if (Array.isArray(data.medications) && data.medications.length) {
      container.append(element("h5", "", "Your medicines"));
      data.medications.forEach((medication) => {
        const row = element("article", "medication-row");
        row.dataset.stopped = String(medication.status === "stopped");
        const heading = element("div", "medication-heading");
        heading.append(element("h6", "", medication.name), element("span", "medication-status", medication.status === "stopped" ? "STOPPED IN SOURCE" : "FROM YOUR RECORD"));
        row.append(heading, element("p", "dose-line", `${medication.dose} ${medication.unit} · ${medication.frequency}`), element("p", "", medication.text));
        container.append(row);
      });
    }
    if (Array.isArray(data.labs) && data.labs.length) {
      container.append(element("h5", "", "Your lab results"));
      const labs = element("div", "lab-grid");
      data.labs.forEach((lab) => {
        const row = element("article", "lab-row");
        const status = String(lab.status || "recorded");
        row.dataset.abnormal = String(!["normal", "in_range", "within range", "within_range"].includes(status.toLowerCase()));
        const heading = element("div", "lab-heading");
        heading.append(element("h6", "", lab.name), element("span", "", status.replaceAll("_", " ")));
        const range = lab.low !== undefined && lab.high !== undefined ? ` · Source range ${lab.low} to ${lab.high} ${lab.unit}` : "";
        row.append(heading, element("p", "lab-value", `${lab.value} ${lab.unit}${range}`), element("p", "", lab.text || ""));
        labs.append(row);
      });
      container.append(labs);
    }
    appendList(container, "Follow-up from your discharge record", data.follow_up);
    appendList(container, "When to get help, as written in your record", data.red_flags);
    const interactions = Array.isArray(result.interactions) ? result.interactions : [];
    if (interactions.length) {
      const warning = element("aside", "interaction-warning");
      warning.append(element("h5", "", "Medication review note"));
      interactions.forEach((interaction) => {
        warning.append(element("p", "", typeof interaction === "string" ? interaction : interaction.message || interaction.warning || interaction.description || JSON.stringify(interaction)));
        if (interaction.source) {
          try {
            const sourceUrl = new URL(interaction.source);
            if (sourceUrl.protocol === "https:") {
              const source = element("a", "interaction-source", "Read the medicine label source ↗");
              source.href = sourceUrl.href;
              source.target = "_blank";
              source.rel = "noopener noreferrer";
              warning.append(source);
            }
          } catch { /* An invalid evidence URL is not rendered as a link. */ }
        }
      });
      container.append(warning);
    }
    container.append(element("p", "interaction-scope", "Interaction checks cover selected synthetic demonstration pairs only. Ask a clinician or pharmacist to review your own medication list."));
    show("explanation", true);
    title.focus({ preventScroll: true });
  }

  function receiptFact(parent, label, value) {
    if (value === undefined || value === null || value === "") return;
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", value));
    parent.append(row);
  }

  function renderVerification(result) {
    const trace = Array.isArray(result.trace) ? result.trace : [];
    const receipts = Array.isArray(result.receipts) ? result.receipts : [];
    const count = Number(result.correction_count || 0);
    const corrected = count > 0;
    show("verification-section", true);
    show("correction-callout", corrected);
    if (corrected) {
      const dosageDrill = trace.some((event) => event.agent === "AdversarialStressInjector" && event.status === "injected");
      byId("correction-title").textContent = dosageDrill ? "Atropos caught the changed dose." : "Atropos caught a source mismatch.";
      byId("correction-copy").textContent = result.status === "verified"
        ? "The draft was refused and handed back for correction. The corrected explanation passed the same safety gate before publication."
        : "The draft was refused and handed back for correction. The final candidate did not pass, so the patient explanation remains withheld.";
    }
    byId("verification-summary").textContent = result.status === "verified"
      ? corrected ? `Verified after ${count} correction${count === 1 ? "" : "s"}` : "Verified on the first check"
      : "Publication refused";
    const list = byId("trace-list");
    list.replaceChildren();
    trace.forEach((event) => {
      const row = element("li");
      row.dataset.status = String(event.status || "complete").toLowerCase();
      const heading = element("h4", "", agentLabels[event.agent] || event.agent || "Verification event");
      if (event.agent) heading.title = event.agent;
      const eventLabel = event.status === "running" ? "started" : String(event.status || "complete").replaceAll("_", " ");
      heading.append(element("span", "trace-status", eventLabel));
      row.append(heading, element("p", "", event.message || event.detail || ""));
      list.append(row);
    });
    if (!trace.length) list.append(element("li", "", "No detailed trace was returned for this request."));
    const last = receipts[receipts.length - 1] || {};
    const facts = byId("receipt-facts");
    facts.replaceChildren();
    receiptFact(facts, "Session", result.session_id);
    receiptFact(facts, "Decisions", receipts.length);
    receiptFact(facts, "Final verdict", last.verdict || last.status || result.status);
    receiptFact(facts, "Chain head", last.hash || last.receipt_hash || last.digest || last.chain_hash || last.sha256);
    const evidence = { session_id: result.session_id, status: result.status, model_mode: result.model_mode, correction_count: count, receipts, trace };
    byId("receipt-json").textContent = JSON.stringify(evidence, null, 2);
    byId("download-receipt").disabled = !receipts.length;
    byId("download-reason").textContent = receipts.length ? "JSON evidence for this run. Source fidelity is not a clinical safety certification." : "The service did not return a receipt. Download is unavailable for this run.";
    byId("verify-receipt").disabled = !receipts.length;
    byId("chain-verification").textContent = receipts.length ? "Check the hashes and links. These receipts are unsigned; they do not prove identity or clinical safety." : "No receipt chain was returned. Verification is unavailable for this run.";
    const execution = result.runtime || result.execution || result.execution_evidence || result.runtime_evidence;
    show("execution-details", Boolean(execution));
    if (execution) byId("execution-json").textContent = JSON.stringify(execution, null, 2);
  }

  async function explain() {
    if (state.busy || state.loading || !state.detail) return;
    resetResults();
    state.busy = true;
    byId("result-panel").setAttribute("aria-busy", "true");
    show("explanation-empty", false);
    show("run-progress", true);
    byId("result-badge").textContent = "CHECK IN PROGRESS";
    updateControls();
    try {
      const result = await api("/api/explain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ packet_id: state.selected, stress: byId("stress-toggle").checked }) });
      state.result = result;
      if (result.model_mode) setRuntime(result.model_mode);
      show("run-progress", false);
      if (result.status === "verified" && result.explanation) {
        byId("result-badge").textContent = "SOURCE FIDELITY VERIFIED";
        byId("result-badge").dataset.status = "verified";
        renderExplanation(result);
      } else {
        byId("result-badge").textContent = "PUBLICATION REFUSED";
        byId("result-badge").dataset.status = "refused";
        byId("run-error-text").textContent = "The candidate did not pass the deterministic source-fidelity check. The patient explanation is withheld. Review the safety trail below.";
        show("run-error", true);
      }
      renderVerification(result);
    } catch (error) {
      show("run-progress", false);
      show("run-error", true);
      byId("result-badge").textContent = "NOT PUBLISHED";
      byId("result-badge").dataset.status = "error";
      byId("run-error-text").textContent = `${error.message} You can retry with the button below.`;
    } finally {
      state.busy = false;
      byId("result-panel").setAttribute("aria-busy", "false");
      updateControls();
      if (window.matchMedia("(max-width: 530px)").matches) {
        byId("result-panel").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      }
    }
  }

  byId("retry-load").addEventListener("click", () => { if (state.packets.length && state.selected) selectPacket(state.selected); else loadPackets(); });
  byId("explain-button").addEventListener("click", explain);
  byId("download-receipt").addEventListener("click", () => {
    if (!state.result?.receipts?.length) return;
    const blob = new Blob([byId("receipt-json").textContent], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = element("a");
    link.href = url;
    link.download = `atropos-receipt-${String(state.result.session_id || "verification").replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 64)}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  byId("verify-receipt").addEventListener("click", async () => {
    if (!state.result?.receipts?.length) return;
    const result = state.result;
    byId("verify-receipt").disabled = true;
    byId("chain-verification").textContent = "Checking this receipt chain. The control becomes available when verification finishes.";
    try {
      const checked = await api("/api/receipts/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ receipts: result.receipts }) });
      if (state.result !== result) return;
      byId("chain-verification").textContent = checked.valid
        ? "Chain integrity verified: the hashes and links match. The unsigned chain does not certify clinical safety or prove identity."
        : "Chain verification failed. The receipts or their links do not match.";
    } catch (error) {
      if (state.result === result) byId("chain-verification").textContent = `${error.message} Chain verification is not complete.`;
    } finally {
      if (state.result === result) byId("verify-receipt").disabled = false;
    }
  });
  byId("try-stress").addEventListener("click", () => {
    const stressPacket = state.packets.find((packet) => packet.adversarial);
    if (stressPacket && !state.busy) selectPacket(stressPacket.packet_id);
  });
  loadHealth();
  loadPackets();
})();
