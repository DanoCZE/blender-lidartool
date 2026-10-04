state.reference = null;
state.referenceMarker = null;

function referencePayload() {
  if (!state.reference) return null;
  return {
    lat: state.reference.lat,
    lon: state.reference.lon,
    rotation: state.reference.rotation,
    scale: state.reference.scale,
    opacity: state.reference.opacity,
    width_m: state.reference.widthM
  };
}

function metersToPx(meters, latlng) {
  const east = destinationPoint(latlng, meters, 90);
  const origin = map.latLngToLayerPoint(latlng);
  const shifted = map.latLngToLayerPoint(east);
  return Math.max(8, Math.hypot(shifted.x - origin.x, shifted.y - origin.y));
}

function destinationPoint(latlng, meters, bearingDeg) {
  const radius = 6378137;
  const distance = meters / radius;
  const bearing = bearingDeg * Math.PI / 180;
  const lat1 = latlng.lat * Math.PI / 180;
  const lon1 = latlng.lng * Math.PI / 180;
  const lat2 = Math.asin(Math.sin(lat1) * Math.cos(distance) + Math.cos(lat1) * Math.sin(distance) * Math.cos(bearing));
  const lon2 = lon1 + Math.atan2(
    Math.sin(bearing) * Math.sin(distance) * Math.cos(lat1),
    Math.cos(distance) - Math.sin(lat1) * Math.sin(lat2)
  );
  return L.latLng(lat2 * 180 / Math.PI, lon2 * 180 / Math.PI);
}

function readReferenceControls() {
  if (!state.reference) return;
  state.reference.rotation = Number(document.getElementById("reference-rotation").value);
  state.reference.scale = Number(document.getElementById("reference-scale").value);
  state.reference.opacity = Number(document.getElementById("reference-opacity").value);
  renderReference();
}

function wrapAngle(degrees) {
  return ((degrees + 180) % 360 + 360) % 360 - 180;
}

function beginReferenceTransform(kind) {
  if (state.mode !== "reference" || state.transform) return;
  if (!state.reference) {
    setStatus("Nejdřív vložte předlohu.");
    return;
  }
  const pointer = state.pointer || map.getCenter();
  const center = L.latLng(state.reference.lat, state.reference.lon);
  const origin = map.latLngToContainerPoint(center);
  const point = map.latLngToContainerPoint(pointer);
  state.transform = {
    kind,
    before: snapshot(),
    lat: state.reference.lat,
    lon: state.reference.lon,
    rotation: state.reference.rotation,
    scale: state.reference.scale,
    pointer,
    angle: pointerAngle(center, pointer),
    distance: Math.max(8, Math.hypot(point.x - origin.x, point.y - origin.y))
  };
  map.dragging.disable();
  map.scrollWheelZoom.disable();
  if (state.referenceMarker && state.referenceMarker.dragging) state.referenceMarker.dragging.disable();
  map.getContainer().style.cursor = "crosshair";
  const labels = { move: "Posun (G)", rotate: "Otáčení (R)", scale: "Měřítko (S)" };
  setStatus(`${labels[kind]}: pohyb myší, levé tlačítko nebo Enter potvrdí, pravé tlačítko nebo Esc zruší.`);
}

function applyReferenceTransform(latlng) {
  const transform = state.transform;
  if (!transform || !state.reference) return;
  if (transform.kind === "move") {
    const start = map.latLngToLayerPoint(transform.pointer);
    const now = map.latLngToLayerPoint(latlng);
    const origin = map.latLngToLayerPoint(L.latLng(transform.lat, transform.lon));
    const next = map.layerPointToLatLng(L.point(origin.x + now.x - start.x, origin.y + now.y - start.y));
    state.reference.lat = next.lat;
    state.reference.lon = next.lng;
  } else if (transform.kind === "rotate") {
    const center = L.latLng(transform.lat, transform.lon);
    state.reference.rotation = wrapAngle(transform.rotation + pointerAngle(center, latlng) - transform.angle);
    document.getElementById("reference-rotation").value = String(Math.round(state.reference.rotation));
  } else {
    const center = L.latLng(transform.lat, transform.lon);
    const origin = map.latLngToContainerPoint(center);
    const point = map.latLngToContainerPoint(latlng);
    const distance = Math.max(8, Math.hypot(point.x - origin.x, point.y - origin.y));
    state.reference.scale = Math.min(8, Math.max(0.05, transform.scale * (distance / transform.distance)));
    document.getElementById("reference-scale").value = String(state.reference.scale);
  }
  renderReference();
}

function finishReferenceTransform(confirm) {
  const transform = state.transform;
  if (!transform) return;
  state.transform = null;
  if (!confirm && state.reference) {
    state.reference.lat = transform.lat;
    state.reference.lon = transform.lon;
    state.reference.rotation = transform.rotation;
    state.reference.scale = transform.scale;
    document.getElementById("reference-rotation").value = String(Math.round(transform.rotation));
    document.getElementById("reference-scale").value = String(transform.scale);
  } else if (confirm) {
    state.history.push(transform.before);
    state.future = [];
    if (state.history.length > 80) state.history.shift();
  }
  if (!state.painting && !state.handle) map.dragging.enable();
  map.scrollWheelZoom.enable();
  renderReference();
  map.getContainer().style.cursor = state.mode === "reference" ? "move" : (state.brush ? "crosshair" : "");
  setStatus(confirm ? "Úprava předlohy je potvrzená." : "Úprava předlohy je zrušená.");
}

document.addEventListener("pointermove", (event) => {
  if (!state.transform) return;
  applyReferenceTransform(map.containerPointToLatLng(map.mouseEventToContainerPoint(event)));
});

let blockReferenceMenu = false;

document.addEventListener("pointerdown", (event) => {
  if (!state.transform) return;
  if (event.button === 1) return;
  if (event.button === 2) blockReferenceMenu = true;
  event.preventDefault();
  event.stopPropagation();
  finishReferenceTransform(event.button !== 2);
}, true);

document.addEventListener("contextmenu", (event) => {
  if (!state.transform && !blockReferenceMenu) return;
  blockReferenceMenu = false;
  event.preventDefault();
}, true);

function pointerAngle(center, latlng) {
  const origin = map.latLngToLayerPoint(center);
  const point = map.latLngToLayerPoint(latlng);
  return Math.atan2(point.x - origin.x, origin.y - point.y) * 180 / Math.PI;
}

function bindReferenceHandles() {
  const root = state.referenceMarker && state.referenceMarker.getElement();
  if (!root) return;
  root.querySelectorAll("[data-handle]").forEach((handle) => {
    L.DomEvent.disableClickPropagation(handle);
    handle.addEventListener("mousedown", (event) => {
      if (state.mode !== "reference" || !state.reference) return;
      L.DomEvent.stopPropagation(event);
      event.preventDefault();
      state.skipClick = true;
      state.handle = handle.dataset.handle;
      remember();
      const center = L.latLng(state.reference.lat, state.reference.lon);
      const pointer = map.mouseEventToLatLng(event);
      state.handleOrigin = {
        scale: state.reference.scale,
        rotation: state.reference.rotation,
        distance: Math.max(map.distance(center, pointer), 1),
        angle: pointerAngle(center, pointer)
      };
      map.dragging.disable();
      if (state.referenceMarker.dragging) state.referenceMarker.dragging.disable();
    });
  });
}

function renderReference() {
  if (!state.reference) {
    if (state.referenceMarker) {
      map.removeLayer(state.referenceMarker);
      state.referenceMarker = null;
    }
    return;
  }
  const editing = state.mode === "reference";
  const center = L.latLng(state.reference.lat, state.reference.lon);
  const widthPx = metersToPx(state.reference.widthM * state.reference.scale, center);
  const heightPx = widthPx * (state.reference.height / state.reference.width);
  const handles = editing
    ? `<i data-handle="nw" class="reference-handle nw"></i>
       <i data-handle="ne" class="reference-handle ne"></i>
       <i data-handle="se" class="reference-handle se"></i>
       <i data-handle="sw" class="reference-handle sw"></i>
       <i data-handle="rotate" class="reference-handle rotate"></i>`
    : "";
  const html = `<div class="reference-frame${editing ? " editing" : ""}" style="transform:rotate(${state.reference.rotation}deg)">
    <img alt="" draggable="false" src="/api/reference?v=${state.reference.version}" style="opacity:${state.reference.opacity}">
    ${handles}
  </div>`;
  const icon = L.divIcon({
    className: "reference-icon",
    html,
    iconSize: [widthPx, heightPx],
    iconAnchor: [widthPx / 2, heightPx / 2]
  });
  if (!state.referenceMarker) {
    state.referenceMarker = L.marker(center, { icon, draggable: editing, pane: "reference" }).addTo(map);
    state.referenceMarker.on("mousedown", () => { state.skipClick = true; });
    state.referenceMarker.on("dragstart", () => remember());
    state.referenceMarker.on("drag", (event) => {
      const latlng = event.target.getLatLng();
      state.reference.lat = latlng.lat;
      state.reference.lon = latlng.lng;
    });
  } else {
    state.referenceMarker.setIcon(icon);
    state.referenceMarker.setLatLng(center);
    if (editing && !state.transform) state.referenceMarker.dragging.enable();
    else state.referenceMarker.dragging.disable();
  }
  bindReferenceHandles();
  applyModeChrome();
}

function placeReference(center, saved) {
  const image = new Image();
  image.onload = () => {
    if (!saved) remember();
    const viewWidth = map.distance(map.getBounds().getNorthWest(), map.getBounds().getNorthEast());
    state.reference = {
      lat: saved ? saved.lat : center.lat,
      lon: saved ? saved.lon : center.lng,
      rotation: saved ? saved.rotation : 0,
      scale: saved ? saved.scale : 1,
      opacity: saved ? saved.opacity : 0.6,
      widthM: saved ? saved.width_m : Math.min(2000, Math.max(80, viewWidth * 0.35)),
      width: image.naturalWidth || 1,
      height: image.naturalHeight || 1,
      version: Date.now()
    };
    document.getElementById("reference-rotation").value = state.reference.rotation;
    document.getElementById("reference-scale").value = state.reference.scale;
    document.getElementById("reference-opacity").value = state.reference.opacity;
    renderReference();
    setStatus("Předloha je na mapě. G posune, R otočí, S mění měřítko.");
  };
  image.onerror = () => setStatus("Předlohu se nepodařilo zobrazit.");
  image.src = `/api/reference?v=${Date.now()}`;
}

function restoreReference(saved) {
  placeReference(map.getCenter(), saved);
}

async function uploadReference(body, headers) {
  const response = await fetch("/api/reference", { method: "POST", headers, body });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Předlohu se nepodařilo vložit.");
  if (state.referenceMarker) {
    map.removeLayer(state.referenceMarker);
    state.referenceMarker = null;
  }
  placeReference(map.getCenter());
}

function sendReferenceFile(file) {
  if (!file) return;
  const step = debugStart("Nahrát předlohu");
  document.getElementById("reference-drop-name").textContent = file.name;
  file.arrayBuffer()
    .then((buffer) => uploadReference(buffer, { "Content-Type": file.type || "application/octet-stream" }))
    .then(() => debugEnd(step))
    .catch((error) => {
      setStatus(error.message);
      debugEnd(step, error);
    });
}

const referenceDrop = document.getElementById("reference-drop");
referenceDrop.onclick = () => document.getElementById("reference-file").click();
document.getElementById("reference-file").addEventListener("change", () => {
  sendReferenceFile(document.getElementById("reference-file").files[0]);
});
["dragenter", "dragover"].forEach((name) => {
  referenceDrop.addEventListener(name, (event) => {
    event.preventDefault();
    referenceDrop.classList.add("dragover");
  });
});
referenceDrop.addEventListener("dragleave", (event) => {
  if (referenceDrop.contains(event.relatedTarget)) return;
  referenceDrop.classList.remove("dragover");
});
referenceDrop.addEventListener("drop", (event) => {
  event.preventDefault();
  referenceDrop.classList.remove("dragover");
  sendReferenceFile(event.dataTransfer.files[0]);
});

["reference-rotation", "reference-scale", "reference-opacity"].forEach((id) => {
  const input = document.getElementById(id);
  input.addEventListener("pointerdown", () => remember());
  input.addEventListener("input", readReferenceControls);
});

document.getElementById("reference-clear").onclick = () => {
  if (!state.reference) return;
  remember();
  state.reference = null;
  renderReference();
};

map.on("zoomend", renderReference);
