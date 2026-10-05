map.createPane("visibility");
map.getPane("visibility").style.zIndex = 450;
map.getPane("visibility").style.pointerEvents = "none";

let visibilityTimer = 0;

function hideVisibility() {
  if (state.visibilityLayer) {
    map.removeLayer(state.visibilityLayer);
    state.visibilityLayer = null;
  }
  document.getElementById("visibility").classList.remove("active");
}

function showVisibility(revision, data) {
  hideVisibility();
  const bounds = [[Number(data.south), Number(data.west)], [Number(data.north), Number(data.east)]];
  const layer = L.imageOverlay(
    `/api/visibility.png?token=${encodeURIComponent(token)}&v=${revision}`,
    bounds,
    { opacity: 1, interactive: false, pane: "visibility" }
  );
  layer.addTo(map);
  state.visibilityLayer = layer;
  document.getElementById("visibility").classList.add("active");
}

function stopVisibilityPoll() {
  clearInterval(visibilityTimer);
  visibilityTimer = 0;
}

function pollVisibility(revision, step) {
  stopVisibilityPoll();
  visibilityTimer = setInterval(async () => {
    try {
      const response = await fetch(`/api/visibility?token=${encodeURIComponent(token)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Viditelnost se nepodařilo načíst.");
      if (Number(data.failed) === revision) {
        stopVisibilityPoll();
        state.visibilityBusy = false;
        debugEnd(step, new Error(data.error || "Viditelnost se nepodařilo spočítat."));
        setStatus(data.error || "Viditelnost se nepodařilo spočítat.");
        return;
      }
      if (Number(data.ready) !== revision || data.south == null) return;
      stopVisibilityPoll();
      showVisibility(revision, data);
      state.visibilityBusy = false;
      debugEnd(step);
      setStatus(data.message || "Zeleně je z tratě vidět.");
    } catch (error) {
      stopVisibilityPoll();
      state.visibilityBusy = false;
      debugEnd(step, error);
      setStatus(error.message || "Viditelnost se nepodařilo načíst.");
    }
  }, 800);
}

document.getElementById("visibility").onclick = async () => {
  if (state.visibilityLayer) {
    hideVisibility();
    setStatus("Viditelnost je skrytá.");
    return;
  }
  if (state.visibilityBusy) return;
  const main = state.segments[0] || [];
  if (main.length < 2) {
    setStatus("Viditelnost potřebuje trať aspoň se dvěma body.");
    return;
  }
  state.visibilityBusy = true;
  const step = debugStart("Viditelnost");
  setStatus("Počítám viditelnost z DMR 5G, včetně vybraných budov.");
  try {
    const response = await fetch("/api/visibility", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        segments: state.segments,
        features: typeof selectionPayload === "function" ? selectionPayload() : []
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Viditelnost se nepodařilo spustit.");
    pollVisibility(Number(data.revision), step);
  } catch (error) {
    state.visibilityBusy = false;
    debugEnd(step, error);
    setStatus(error.message || "Viditelnost se nepodařilo spustit.");
  }
};
