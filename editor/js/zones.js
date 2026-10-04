function brushRadius() {
  const value = Number(document.getElementById("brush-radius").value);
  return Math.min(200, Math.max(2, Number.isFinite(value) ? value : 24));
}

function brushTarget() {
  const node = document.getElementById("brush-target");
  const value = node ? node.value : "1";
  return value === "2" || value === "3" || value === "4" || value === "corridor" ? value : "1";
}

function stampTarget(stamp) {
  const value = stamp && stamp.target;
  return value === "2" || value === "3" || value === "4" || value === "corridor" ? value : "1";
}

function brushColor(target) {
  if (target === "corridor") return "#d7a441";
  return zoneColors[(Number(target) || 1) - 1] || zoneColors[0];
}

function brushLabel(target) {
  return target === "corridor" ? "koridor silnice" : `zóna ${target}`;
}

function applyBrushChrome() {
  const target = brushTarget();
  const color = brushColor(target);
  const menu = document.getElementById("brush-menu");
  if (menu) menu.style.setProperty("--brush", color);
  const button = document.getElementById("brush");
  if (button) {
    button.title = `Štětec: ${brushLabel(target)} (Tab). Najetím nastavíte cíl a poloměr. Tažením rozšíříte plochu, Ctrl maže, Alt a kolečko mění velikost.`;
  }
  if (state.brushCursor) {
    state.brushCursor.setStyle({ color, fillColor: color });
  }
}

function setBrushRadius(value) {
  const radius = Math.min(200, Math.max(2, Math.round(Number(value) || 24)));
  document.getElementById("brush-radius").value = String(radius);
  document.getElementById("brush-badge").textContent = String(radius);
  if (state.brushCursor) state.brushCursor.setRadius(radius);
}

function toggleBrush() {
  if (state.mode !== "track") setMode("track");
  state.brush = !state.brush;
  document.getElementById("brush").classList.toggle("active", state.brush);
  map.getContainer().style.cursor = state.brush ? "crosshair" : "";
  applyBrushChrome();
  if (!state.brush && state.brushCursor) {
    map.removeLayer(state.brushCursor);
    state.brushCursor = null;
  }
}

function describe() {
  const points = state.segments.reduce((sum, segment) => sum + segment.length, 0);
  const active = pointRefValid(state.selectedPoint) ? " Aktivní bod: Shift a klik z něj udělá křižovatku." : "";
  setStatus(points ? `${points} bodů.${active}` : "Shift a klik přidá první bod. Klik na bod ho označí.");
}

function readZones() {
  const zones = [];
  for (let index = 1; index <= 4; index += 1) {
    const radiusText = document.getElementById(`radius-${index}`).value.trim();
    zones.push({
      id: index,
      radius_m: radiusText === "" ? null : Number(radiusText),
      step_m: Number(document.getElementById(`step-${index}`).value)
    });
  }
  return zones;
}

function fillZones(zones, terrainBuffer, corridor) {
  document.getElementById("terrain-buffer").value = terrainBuffer;
  if (corridor != null) document.getElementById("road-corridor").value = corridor;
  zones.forEach((zone) => {
    document.getElementById(`radius-${zone.id}`).value = zone.radius_m == null ? "" : zone.radius_m;
    document.getElementById(`step-${zone.id}`).value = zone.step_m;
  });
}

function zonesError(zones) {
  let last = 0;
  for (const zone of zones) {
    if (!(zone.step_m > 0)) return "Krok zóny musí být větší než nula.";
    if (zone.radius_m == null || zone.radius_m === 0) continue;
    if (!Number.isFinite(zone.radius_m) || zone.radius_m < 0) return "Poloměr zóny je neplatný.";
    if (zone.radius_m < last) return "Poloměry zón musí růst od silnice ven.";
    last = zone.radius_m;
  }
  const buffer = Number(document.getElementById("terrain-buffer").value);
  if (!(buffer > 0)) return "Hranice terénu musí být větší než nula.";
  const corridor = Number(document.getElementById("road-corridor").value);
  if (!(corridor >= 5)) return "Koridor silnice musí být aspoň 5 m.";
  return "";
}

function trackFeature() {
  const lines = state.segments.filter((segment) => segment.length >= 2).map((segment) => segment.map((point) => [point.lon, point.lat]));
  if (!lines.length) return null;
  if (lines.length === 1) return turf.lineString(lines[0]);
  return turf.multiLineString(lines);
}

function terrainRectangle(points, bufferM) {
  const lats = points.map((point) => point.lat);
  const lons = points.map((point) => point.lon);
  const lat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const metersPerDegree = 111320;
  const dLat = bufferM / metersPerDegree;
  const dLon = bufferM / (metersPerDegree * Math.cos(lat * Math.PI / 180));
  const west = Math.min(...lons) - dLon;
  const east = Math.max(...lons) + dLon;
  const south = Math.min(...lats) - dLat;
  const north = Math.max(...lats) + dLat;
  return turf.polygon([[[west, south], [east, south], [east, north], [west, north], [west, south]]]);
}

function drawZones() {
  if (state.zoneLayer) {
    map.removeLayer(state.zoneLayer);
    state.zoneLayer = null;
  }
  const points = state.segments.flat();
  if (points.length < 2 || typeof turf === "undefined") return;
  const line = trackFeature();
  if (!line) return;
  const problem = zonesError(readZones());
  if (problem) return;
  let rect;
  try {
    rect = terrainRectangle(points, Number(document.getElementById("terrain-buffer").value));
  } catch (error) {
    return;
  }
  state.zoneLayer = L.layerGroup().addTo(map);
  const zones = readZones().slice().sort((left, right) => {
    const leftRadius = left.radius_m == null || left.radius_m === 0 ? 1e12 : left.radius_m;
    const rightRadius = right.radius_m == null || right.radius_m === 0 ? 1e12 : right.radius_m;
    return rightRadius - leftRadius;
  });
  zones.forEach((zone) => {
    let geom = rect;
    if (zone.radius_m != null && zone.radius_m > 0) {
      try {
        geom = turf.intersect(rect, turf.buffer(line, zone.radius_m, { units: "meters" }));
      } catch (error) {
        geom = null;
      }
    }
    if (!geom) return;
    const color = zoneColors[(zone.id - 1) % zoneColors.length];
    const radius = zone.radius_m == null || zone.radius_m === 0 ? "zbytek terénu" : `${zone.radius_m} m`;
    L.geoJSON(geom, { style: { color, weight: 2, fillColor: color, fillOpacity: 0.22 } })
      .bindTooltip(`Zóna ${zone.id}: ${radius}, krok ${zone.step_m} m`)
      .addTo(state.zoneLayer);
  });
  const corridorM = Number(document.getElementById("road-corridor").value);
  let corridor = null;
  if (corridorM >= 5) {
    try {
      corridor = turf.intersect(rect, turf.buffer(line, corridorM, { units: "meters" }));
    } catch (error) {
      corridor = null;
    }
  }
  if (corridor) {
    L.geoJSON(corridor, { style: { color: "#d7a441", weight: 2, fillColor: "#d7a441", fillOpacity: 0.28 } })
      .bindTooltip(`Koridor silnice: ${corridorM} m`)
      .addTo(state.zoneLayer);
  }
  bringTrackFront();
}

function draw() {
  if (state.markers) map.removeLayer(state.markers);
  if (state.line) map.removeLayer(state.line);
  if (state.extras) map.removeLayer(state.extras);
  state.markers = null;
  state.line = null;
  state.extras = null;
  const points = main();
  if (points.length >= 2) {
    state.line = L.polyline(points.map((point) => [point.lat, point.lon]), { color: "#f2f2f2", weight: 4, interactive: false }).addTo(map);
  }
  const extra = state.segments.slice(1).filter((segment) => segment.length >= 2);
  if (extra.length) {
    state.extras = L.featureGroup(extra.map((segment) => L.polyline(segment.map((point) => [point.lat, point.lon]), {
      color: "#7ddec0", weight: 4, interactive: false
    }))).addTo(map);
  }
  state.markers = L.featureGroup().addTo(map);
  if (!pointRefValid(state.selectedPoint)) state.selectedPoint = null;
  const drawn = [];
  const seen = new Set();
  state.segments.forEach((segment, segmentIndex) => {
    segment.forEach((point, index) => {
      if (seen.has(point)) return;
      seen.add(point);
      drawn.push({ point, segmentIndex, index });
    });
  });
  drawn.sort((left, right) => Number(samePoint(state.selectedPoint, left.segmentIndex, left.index)) - Number(samePoint(state.selectedPoint, right.segmentIndex, right.index)));
  if (drawn.length <= 500) {
    drawn.forEach(({ point, segmentIndex, index }) => {
      const selected = samePoint(state.selectedPoint, segmentIndex, index);
      const marker = L.circleMarker([point.lat, point.lon], {
        pane: "points",
        bubblingMouseEvents: false,
        radius: selected ? 12 : 9,
        color: selected ? "#d97706" : "#1c1c1c",
        fillColor: selected ? "#ffe1b0" : (segmentIndex === 0 && index === 0 ? "#3d8bfd" : "#ffffff"),
        weight: selected ? 4 : 2,
        fillOpacity: 1
      });
      marker.addTo(state.markers);
      enableDrag(marker, segmentIndex, index);
    });
  }
  updateDeleteButton();
  try {
    drawZones();
  } finally {
    drawBrushes();
  }
  describe();
}

function brushArea(target) {
  const stamps = state.brushes.filter((stamp) => stampTarget(stamp) === target);
  if (!stamps.length || typeof turf === "undefined") return null;
  let area = null;
  for (const stamp of stamps) {
    let circle;
    try {
      circle = turf.circle([stamp.lon, stamp.lat], stamp.radius_m, { steps: 28, units: "meters" });
    } catch (error) {
      continue;
    }
    try {
      if (stamp.erase) area = area ? turf.difference(area, circle) : null;
      else area = area ? turf.union(area, circle) : circle;
    } catch (error) {
      continue;
    }
  }
  return area;
}

function drawBrushes() {
  if (state.brushLayer) map.removeLayer(state.brushLayer);
  state.brushLayer = null;
  if (typeof turf === "undefined") return;
  const layers = [];
  ["4", "3", "2", "1", "corridor"].forEach((target) => {
    const area = brushArea(target);
    if (!area) return;
    const color = brushColor(target);
    layers.push(L.geoJSON(area, {
      interactive: false,
      style: { color, weight: 1.5, fillColor: color, fillOpacity: 0.38 }
    }));
  });
  if (!layers.length) return;
  state.brushLayer = L.layerGroup(layers).addTo(map);
  bringTrackFront();
}

function bringTrackFront() {
  [state.line, state.extras, state.markers].forEach((layer) => {
    if (layer && typeof layer.bringToFront === "function") layer.bringToFront();
  });
}

document.getElementById("zones-panel").addEventListener("input", () => drawZones());
document.getElementById("zone-rows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-radius]");
  if (!button) return;
  const input = document.getElementById(`radius-${button.dataset.radius}`);
  const current = input.value.trim() === "" ? 0 : Number(input.value);
  const next = Math.max(0, (Number.isFinite(current) ? current : 0) + Number(button.dataset.delta));
  input.value = next === 0 ? "" : String(next);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});
