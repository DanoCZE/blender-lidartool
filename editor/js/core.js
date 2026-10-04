const params = new URLSearchParams(location.search);
const token = params.get("token") || "";
const zoneColors = ["#ffb020", "#3d8bfd", "#c39bd3", "#8fd0b5"];
const state = {
  segments: [[]], brushes: [], history: [], future: [], selectedPoint: null, drag: false, skipClick: false, brush: false, painting: false, mode: "track",
  markers: null, line: null, extras: null, zoneLayer: null, brushLayer: null, brushCursor: null
};
const zoneRows = document.getElementById("zone-rows");
for (let index = 1; index <= 4; index += 1) {
  const block = document.createElement("div");
  block.innerHTML = `<div class="zone-title"><i class="swatch" style="background:${zoneColors[index - 1]}"></i>Zóna ${index}</div>
    <label>Poloměr (m)<span class="stepper"><button type="button" class="btn icon circle" data-radius="${index}" data-delta="-1" title="Zmenšit poloměr o 1 m.">−</button><input id="radius-${index}" type="number" min="0" step="1" placeholder="zbytek"><button type="button" class="btn icon circle" data-radius="${index}" data-delta="1" title="Zvětšit poloměr o 1 m.">+</button></span></label>
    <label>Krok (m)<input id="step-${index}" type="number" min="0.5" step="0.5"></label>`;
  zoneRows.appendChild(block);
}

const map = L.map("map").setView([49.8, 15.5], 8);
map.createPane("reference");
map.getPane("reference").style.zIndex = 350;
map.createPane("points");
map.getPane("points").style.zIndex = 650;
map.getPane("points").style.pointerEvents = "none";
const ortho = L.tileLayer("https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO_WM/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 20, attribution: "© ČÚZK"
});
const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap"
});
ortho.addTo(map);

function setBasemap(name) {
  const osmOn = name === "osm";
  if (osmOn) {
    if (map.hasLayer(ortho)) map.removeLayer(ortho);
    if (!map.hasLayer(osm)) osm.addTo(map);
  } else {
    if (map.hasLayer(osm)) map.removeLayer(osm);
    if (!map.hasLayer(ortho)) ortho.addTo(map);
  }
  document.getElementById("basemap-ortho").classList.toggle("active", !osmOn);
  document.getElementById("basemap-osm").classList.toggle("active", osmOn);
}

document.getElementById("basemap-ortho").onclick = () => setBasemap("ortho");
document.getElementById("basemap-osm").onclick = () => setBasemap("osm");

const status = document.getElementById("status");
const setStatus = (text) => { status.textContent = text; };
const debugLog = [];
let debugSerial = 0;

function debugStamp(date) {
  return date.toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderDebug() {
  const lines = document.getElementById("debug-lines");
  const badge = document.getElementById("debug-badge");
  const running = debugLog.some((item) => item.state === "run");
  const failed = debugLog.some((item) => item.state === "error");
  badge.classList.toggle("hidden", !running && !failed);
  badge.style.background = failed ? "#e85d4c" : "#ffb020";
  if (!debugLog.length) {
    lines.className = "debug-empty";
    lines.textContent = "Zatím žádný krok.";
    return;
  }
  lines.className = "";
  lines.innerHTML = "";
  debugLog.forEach((item) => {
    const row = document.createElement("div");
    row.className = `debug-line ${item.state}`;
    const when = document.createElement("span");
    when.textContent = item.time;
    const text = document.createElement("span");
    if (item.state === "run") text.textContent = `${item.name}: běží`;
    else if (item.state === "ok") text.textContent = `${item.name}: hotovo`;
    else text.textContent = `${item.name}: ${item.detail}`;
    row.append(when, text);
    lines.appendChild(row);
  });
  lines.parentElement.scrollTop = lines.parentElement.scrollHeight;
}

function debugStart(name) {
  const id = ++debugSerial;
  debugLog.push({ id, name, state: "run", detail: "", time: debugStamp(new Date()) });
  if (debugLog.length > 80) debugLog.shift();
  renderDebug();
  return id;
}

function debugEnd(id, error) {
  const item = debugLog.find((entry) => entry.id === id);
  if (!item) return;
  item.state = error ? "error" : "ok";
  item.detail = error ? String(error.message || error) : "";
  item.time = debugStamp(new Date());
  renderDebug();
}

function setControlDisabled(el, value) {
  if (!el) return;
  el.disabled = value;
  el.toggleAttribute("disabled", !!value);
}

document.getElementById("debug-toggle").onclick = () => {
  const consolePanel = document.getElementById("debug-console");
  consolePanel.classList.toggle("hidden");
  document.getElementById("debug-toggle").classList.toggle("active", !consolePanel.classList.contains("hidden"));
  renderDebug();
};

const sidebar = document.getElementById("zones-panel");
const sidebarToggle = document.getElementById("sidebar-toggle");
function setSidebarCollapsed(collapsed) {
  sidebar.classList.toggle("collapsed", collapsed);
  sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
  sidebarToggle.title = collapsed ? "Rozbalit panel" : "Sbalit panel";
}
sidebarToggle.onclick = () => setSidebarCollapsed(!sidebar.classList.contains("collapsed"));
