window.addEventListener("keydown", (event) => {
  const typing = event.target && event.target.closest("input, textarea, select");
  if (helpIsOpen()) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeHelp();
    } else if (event.key === "Tab") {
      event.preventDefault();
    }
    return;
  }
  if (typing) return;
  if (event.key === "Tab" && !event.repeat) {
    event.preventDefault();
    toggleBrush();
    return;
  }
  if (state.transform) {
    if (event.key === "Escape") {
      event.preventDefault();
      finishReferenceTransform(false);
    } else if (event.key === "Enter") {
      event.preventDefault();
      finishReferenceTransform(true);
    } else if (!event.ctrlKey && !event.metaKey) {
      event.preventDefault();
    }
    return;
  }
  const plain = !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey && !event.repeat;
  if (plain && state.mode === "reference" && (event.key === "g" || event.key === "G" || event.key === "r" || event.key === "R" || event.key === "s" || event.key === "S")) {
    event.preventDefault();
    const kind = event.key.toLowerCase() === "g" ? "move" : (event.key.toLowerCase() === "r" ? "rotate" : "scale");
    beginReferenceTransform(kind);
    return;
  }
  if ((event.key === "Delete" || event.key === "Backspace") && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault();
    deleteSelectedPoint();
    return;
  }
  const key = event.key.toLowerCase();
  if (!(event.ctrlKey || event.metaKey)) return;
  if (key === "z" && !event.shiftKey) {
    event.preventDefault();
    undo();
  } else if (key === "y" || (key === "z" && event.shiftKey)) {
    event.preventDefault();
    redo();
  }
});

async function editTrack(action) {
  if (state.busyEdit) return;
  const before = snapshot();
  repairMainLine();
  const stepName = action === "simplify" ? "Zjednodušit" : "Připnout na silnice";
  if (!state.segments.some((segment) => Array.isArray(segment) && segment.length >= 2)) {
    restore(before);
    setStatus("Trať potřebuje aspoň dva body.");
    debugEnd(debugStart(stepName), "Trať potřebuje aspoň dva body.");
    return;
  }
  state.busyEdit = true;
  setControlDisabled(document.getElementById("simplify"), true);
  setControlDisabled(document.getElementById("snap"), true);
  setStatus(action === "simplify" ? "Zjednodušuji trať." : "Připínám trať na silnice.");
  const step = debugStart(stepName);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 50000);
  try {
    const response = await fetch("/api/track/edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        token,
        action,
        segments: state.segments.filter((segment) => segment.length >= 2),
        profile: document.getElementById("profile").value
      })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Úprava tratě selhala.");
    state.history.push(before);
    state.future = [];
    if (state.history.length > 80) state.history.shift();
    state.segments = data.segments && data.segments.length ? data.segments : [[]];
    state.selectedPoint = null;
    state.junctionAnchor = null;
    draw();
    fit();
    setStatus(data.message || "Trať je upravená.");
    debugEnd(step);
  } catch (error) {
    restore(before);
    const aborted = error && (error.name === "AbortError" || error.message === "The user aborted a request.");
    const message = aborted
      ? (action === "simplify"
        ? "Zjednodušení trvalo příliš dlouho."
        : "Přichycení trvalo příliš dlouho. Zkuste kratší úsek nebo profil chůze.")
      : error.message;
    setStatus(message);
    debugEnd(step, aborted ? message : error);
  } finally {
    clearTimeout(timer);
    state.busyEdit = false;
    setControlDisabled(document.getElementById("simplify"), false);
    setControlDisabled(document.getElementById("snap"), false);
  }
}

document.getElementById("simplify").onclick = () => editTrack("simplify");
document.getElementById("snap").onclick = () => editTrack("snap");

document.getElementById("clear").onclick = () => {
  if (!state.segments.flat().length) return;
  remember();
  state.segments = [[]];
  draw();
};

document.getElementById("apply").onclick = async () => {
  const zones = readZones();
  const problem = zonesError(zones);
  if (repairMainLine()) draw();
  if (!state.segments.some((segment) => Array.isArray(segment) && segment.length >= 2)) {
    setStatus("Trať potřebuje aspoň dva body.");
    debugEnd(debugStart("Přenést do Blenderu"), "Trať potřebuje aspoň dva body.");
    return;
  }
  if (problem) {
    setStatus(problem);
    debugEnd(debugStart("Přenést do Blenderu"), problem);
    return;
  }
  const step = debugStart("Přenést do Blenderu");
  try {
    const response = await fetch("/api/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        segments: state.segments.filter((segment) => segment.length >= 2),
        zones,
        terrain_buffer_m: Number(document.getElementById("terrain-buffer").value),
        road_buffer_m: Number(document.getElementById("road-corridor").value),
        zone1_brush: state.brushes,
        reference: referencePayload()
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Uložení selhalo.");
    setStatus(data.message || "Trať je ve scéně Blenderu.");
    debugEnd(step);
    window.close();
  } catch (error) {
    setStatus(error.message);
    debugEnd(step, error);
  }
};
