const BUILDING_ZOOM = 16;
const BUILDING_LIMIT = 200;

state.buildingSelection = new Map();
state.buildingsBusy = false;

map.createPane("buildings");
map.getPane("buildings").style.zIndex = 560;

function buildingStyle(selected) {
  return selected
    ? { color: "#ffb020", weight: 2, fillColor: "#ffb020", fillOpacity: 0.55 }
    : { color: "#d7e4f5", weight: 1, fillColor: "#8fb4d9", fillOpacity: 0.2 };
}

function updateBuildingPanel() {
  const count = state.buildingSelection.size;
  const label = document.getElementById("buildings-count");
  if (label) label.textContent = `Vybrané budovy: ${count}`;
  const insert = document.getElementById("buildings-insert");
  if (insert) insert.disabled = count === 0 || state.buildingsBusy;
}

function clearBuildingLayer() {
  if (state.buildingLayer) {
    map.removeLayer(state.buildingLayer);
    state.buildingLayer = null;
  }
}

function restyleBuildings() {
  if (!state.buildingLayer) return;
  state.buildingLayer.eachLayer((layer) => {
    const id = layer.feature && layer.feature.properties && layer.feature.properties.id;
    layer.setStyle(buildingStyle(state.buildingSelection.has(id)));
  });
}

function buildingFeature(item) {
  return {
    type: "Feature",
    properties: { id: item.id, name: item.name || "", kind: item.kind || "" },
    geometry: item.geometry
  };
}

function restoreBuildingSelection(items) {
  state.buildingSelection = new Map();
  (items || []).forEach((item) => {
    if (!item || !item.id || !item.geometry) return;
    state.buildingSelection.set(String(item.id), buildingFeature(item));
  });
  restyleBuildings();
  updateBuildingPanel();
}

function selectionPayload() {
  return [...state.buildingSelection.values()].map((feature) => ({
    id: feature.properties.id,
    name: feature.properties.name || "",
    kind: feature.properties.kind || "",
    geometry: feature.geometry
  }));
}

let selectionSerial = 0;

function saveBuildingSelection(beacon) {
  const serial = ++selectionSerial;
  const body = JSON.stringify({ token, features: selectionPayload(), serial });
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon("/api/buildings/selection", new Blob([body], { type: "application/json" }));
    return;
  }
  fetch("/api/buildings/selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body
  }).then((response) => response.json().then((data) => ({ ok: response.ok, data }))).then(({ ok, data }) => {
    if (!ok) setStatus(data.detail || "Výběr budov se nepodařilo uložit.");
  }).catch(() => setStatus("Výběr budov se nepodařilo uložit."));
}

window.addEventListener("pagehide", () => saveBuildingSelection(true));

function toggleBuilding(feature) {
  const id = feature.properties && feature.properties.id;
  if (!id) return;
  if (state.buildingSelection.has(id)) {
    state.buildingSelection.delete(id);
  } else if (state.buildingSelection.size >= BUILDING_LIMIT) {
    setStatus(`Najednou jde vložit nejvýš ${BUILDING_LIMIT} budov.`);
    return;
  } else {
    state.buildingSelection.set(id, feature);
  }
  restyleBuildings();
  updateBuildingPanel();
  saveBuildingSelection(false);
}

function drawBuildingLayer(features) {
  clearBuildingLayer();
  features.forEach((feature) => {
    const id = feature.properties && feature.properties.id;
    if (id && state.buildingSelection.has(id)) state.buildingSelection.set(id, feature);
  });
  state.buildingLayer = L.geoJSON({ type: "FeatureCollection", features }, {
    pane: "buildings",
    interactive: true,
    style: (feature) => buildingStyle(state.buildingSelection.has(feature.properties.id)),
    onEachFeature: (feature, layer) => {
      layer.on("click", (event) => {
        L.DomEvent.stop(event);
        toggleBuilding(feature);
      });
    }
  }).addTo(map);
  updateBuildingPanel();
}

let buildingLoad = 0;
let buildingTimer = 0;

function scheduleBuildings() {
  clearTimeout(buildingTimer);
  buildingTimer = setTimeout(loadBuildings, 400);
}

async function loadBuildings() {
  if (state.mode !== "buildings") return;
  if (map.getZoom() < BUILDING_ZOOM) {
    clearBuildingLayer();
    setStatus("Přibližte mapu, ať se načtou půdorysy budov.");
    return;
  }
  const bounds = map.getBounds();
  const serial = ++buildingLoad;
  const step = debugStart("Půdorysy");
  try {
    const params = new URLSearchParams({
      token,
      minx: String(bounds.getWest()),
      miny: String(bounds.getSouth()),
      maxx: String(bounds.getEast()),
      maxy: String(bounds.getNorth())
    });
    const response = await fetch(`/api/buildings?${params.toString()}`);
    const data = await response.json();
    if (serial !== buildingLoad || state.mode !== "buildings") return;
    if (!response.ok) throw new Error(data.detail || "Půdorysy se nepodařilo načíst.");
    drawBuildingLayer(data.features || []);
    debugEnd(step);
    if (data.truncated) setStatus("Ve výřezu je moc budov. Přibližte mapu, ať jdou vybrat po jedné.");
    else setStatus("Kliknutím vyberete budovu. Vložit budovy je přidá do Blenderu.");
  } catch (error) {
    if (serial !== buildingLoad) return;
    debugEnd(step, error);
    setStatus(error.message || "Půdorysy se nepodařilo načíst.");
  }
}

function syncBuildingLayer() {
  if (state.mode !== "buildings") {
    clearBuildingLayer();
    return;
  }
  loadBuildings();
}

map.on("moveend", () => {
  if (state.mode === "buildings") scheduleBuildings();
});

document.getElementById("buildings-clear").onclick = () => {
  if (!state.buildingSelection.size) return;
  state.buildingSelection.clear();
  restyleBuildings();
  updateBuildingPanel();
  saveBuildingSelection(false);
  setStatus("Výběr budov je zrušený.");
};

document.getElementById("buildings-insert").onclick = async () => {
  const features = [...state.buildingSelection.values()].map((feature) => ({
    id: feature.properties.id,
    name: feature.properties.name || "",
    kind: feature.properties.kind || "",
    geometry: feature.geometry
  }));
  if (!features.length || state.buildingsBusy) return;
  state.buildingsBusy = true;
  updateBuildingPanel();
  const step = debugStart("Vložit budovy");
  try {
    const response = await fetch("/api/buildings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, features })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Budovy se nepodařilo odeslat.");
    debugEnd(step);
    setStatus(data.message || "Budovy se připravují ve Blenderu.");
  } catch (error) {
    debugEnd(step, error);
    setStatus(error.message || "Budovy se nepodařilo odeslat.");
  } finally {
    state.buildingsBusy = false;
    updateBuildingPanel();
  }
};

updateBuildingPanel();
