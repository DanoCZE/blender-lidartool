function samePoint(selection, segmentIndex, index) {
  return !!selection && selection.segment === segmentIndex && selection.index === index;
}

function pointRefValid(selection) {
  if (!selection) return false;
  const segment = state.segments[selection.segment];
  return !!segment && selection.index >= 0 && selection.index < segment.length;
}

function updateDeleteButton() {
  const button = document.getElementById("delete-point");
  setControlDisabled(button, !pointRefValid(state.selectedPoint) || state.mode !== "track");
}

function deleteSelectedPoint() {
  if (!pointRefValid(state.selectedPoint) || state.mode !== "track") return;
  remember();
  const segment = state.segments[state.selectedPoint.segment];
  segment.splice(state.selectedPoint.index, 1);
  if (segment.length === 0 && state.selectedPoint.segment > 0) {
    state.segments.splice(state.selectedPoint.segment, 1);
  }
  state.selectedPoint = null;
  state.junctionAnchor = null;
  draw();
}

function nearestPoint(latlng, maxPx = 18) {
  const click = map.latLngToContainerPoint(latlng);
  const seen = new Set();
  let best = null;
  let bestDist = maxPx;
  state.segments.forEach((segment, segmentIndex) => {
    segment.forEach((point, index) => {
      if (!point || seen.has(point)) return;
      seen.add(point);
      const dist = click.distanceTo(map.latLngToContainerPoint([point.lat, point.lon]));
      if (dist <= bestDist) {
        bestDist = dist;
        best = { segment: segmentIndex, index };
      }
    });
  });
  return best;
}

function selectPoint(segmentIndex, index) {
  state.selectedPoint = { segment: segmentIndex, index };
  state.junctionAnchor = null;
  draw();
}

function enableDrag(marker, segmentIndex, index) {
  marker.on("click", (event) => {
    L.DomEvent.stop(event);
    if (state.drag) return;
    state.skipClick = true;
    selectPoint(segmentIndex, index);
    setTimeout(() => { state.skipClick = false; }, 0);
  });
  marker.on("mousedown", (event) => {
    if (!event.originalEvent || event.originalEvent.button !== 0) return;
    L.DomEvent.stopPropagation(event);
    const startXY = { x: event.originalEvent.clientX, y: event.originalEvent.clientY };
    let dragging = false;
    const move = (moveEvent) => {
      const original = moveEvent.originalEvent;
      if (!original || dragging) {
        if (dragging) marker.setLatLng(moveEvent.latlng);
        return;
      }
      if (Math.hypot(original.clientX - startXY.x, original.clientY - startXY.y) < 5) return;
      dragging = true;
      state.drag = true;
      state.skipClick = true;
      remember();
      map.dragging.disable();
      marker.setLatLng(moveEvent.latlng);
    };
    const up = (domEvent) => {
      if (up.done) return;
      up.done = true;
      map.off("mousemove", move);
      document.removeEventListener("mouseup", up);
      if (!dragging) return;
      state.drag = false;
      map.dragging.enable();
      const latlng = map.mouseEventToLatLng(domEvent);
      const point = state.segments[segmentIndex] && state.segments[segmentIndex][index];
      if (point) {
        point.lat = latlng.lat;
        point.lon = latlng.lng;
      }
      selectPoint(segmentIndex, index);
      setTimeout(() => { state.skipClick = false; }, 0);
    };
    map.on("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

function fit() {
  const points = state.segments.flat().filter((point) => point && Number.isFinite(point.lat) && Number.isFinite(point.lon));
  if (!points.length) return;
  map.invalidateSize();
  if (points.length === 1) {
    map.setView([points[0].lat, points[0].lon], 16);
    return;
  }
  const size = map.getSize();
  const padRight = Math.min(300, Math.max(24, size.x * 0.28));
  const padTop = Math.min(72, Math.max(24, size.y * 0.12));
  map.fitBounds(points.map((point) => [point.lat, point.lon]), {
    paddingTopLeft: [48, padTop],
    paddingBottomRight: [padRight, 24],
    maxZoom: 17,
    animate: false
  });
}

function fitWhenReady() {
  map.whenReady(() => fit());
  requestAnimationFrame(() => fit());
  setTimeout(() => fit(), 250);
}

function stampBrush(latlng) {
  const radius = brushRadius();
  const target = brushTarget();
  const erase = state.erasing;
  const last = state.brushes[state.brushes.length - 1];
  if (last && stampTarget(last) === target && !!last.erase === erase && map.distance(L.latLng(last.lat, last.lon), latlng) < radius * 0.45) return;
  if (!state.strokeStarted) {
    remember();
    state.strokeStarted = true;
  }
  state.brushes.push({ lat: latlng.lat, lon: latlng.lng, radius_m: radius, erase, target });
  drawBrushes();
}

function applyModeChrome() {
  const editing = state.mode === "reference";
  const referencePane = map.getPane("reference");
  if (referencePane) referencePane.style.zIndex = editing ? "620" : "350";
  const pointsPane = map.getPane("points");
  if (pointsPane) pointsPane.style.zIndex = editing ? "420" : "650";
  const overlay = map.getPane("overlayPane");
  const markers = map.getPane("markerPane");
  if (overlay) overlay.style.opacity = editing ? "0.5" : "1";
  if (markers) markers.style.opacity = editing ? "0.5" : "1";
}

function setMode(mode) {
  if (state.transform && mode !== "reference") finishReferenceTransform(false);
  state.mode = mode;
  document.getElementById("mode-track").classList.toggle("active", mode === "track");
  document.getElementById("mode-reference").classList.toggle("active", mode === "reference");
  document.getElementById("panel-zones").classList.toggle("hidden", mode !== "track");
  document.getElementById("panel-reference").classList.toggle("hidden", mode !== "reference");
  document.getElementById("bar-track").classList.toggle("hidden", mode !== "track");
  document.getElementById("sep-track").classList.toggle("hidden", mode !== "track");
  const sidebarTitle = document.getElementById("sidebar-title");
  if (sidebarTitle) sidebarTitle.textContent = mode === "reference" ? "Předloha" : "Zóny";
  applyModeChrome();
  renderReference();
  updateDeleteButton();
  map.getContainer().style.cursor = mode === "reference" ? "move" : (state.brush ? "crosshair" : "");
  setStatus(mode === "reference" ? "Upravujete předlohu. G posune, R otočí, S mění měřítko." : "Upravujete trať. Klik označí bod, Shift a klik z něj udělá křižovatku.");
}

document.getElementById("mode-track").onclick = () => setMode("track");
document.getElementById("mode-reference").onclick = () => setMode("reference");

document.getElementById("brush").onclick = () => toggleBrush();
document.getElementById("brush-radius").addEventListener("input", (event) => setBrushRadius(event.target.value));
document.getElementById("brush-target").addEventListener("change", () => applyBrushChrome());

document.getElementById("brush-clear").onclick = () => {
  const target = brushTarget();
  if (!state.brushes.some((stamp) => stampTarget(stamp) === target)) return;
  remember();
  state.brushes = state.brushes.filter((stamp) => stampTarget(stamp) !== target);
  drawBrushes();
};

applyBrushChrome();

map.on("mousemove", (event) => {
  state.pointer = event.latlng;
  if (state.transform) {
    applyReferenceTransform(event.latlng);
    return;
  }
  if (state.handle && state.reference && state.handleOrigin) {
    const center = L.latLng(state.reference.lat, state.reference.lon);
    if (state.handle === "rotate") {
      const angle = pointerAngle(center, event.latlng);
      state.reference.rotation = wrapAngle(state.handleOrigin.rotation + (angle - state.handleOrigin.angle));
      document.getElementById("reference-rotation").value = String(Math.round(state.reference.rotation));
    } else {
      const distance = map.distance(center, event.latlng);
      state.reference.scale = Math.min(8, Math.max(0.05, state.handleOrigin.scale * (distance / state.handleOrigin.distance)));
      document.getElementById("reference-scale").value = String(state.reference.scale);
    }
    renderReference();
    return;
  }
  if (!state.brush || state.mode !== "track") return;
  const radius = brushRadius();
  const color = brushColor(brushTarget());
  if (!state.brushCursor) {
    state.brushCursor = L.circle(event.latlng, {
      radius, color, weight: 1, fillColor: color, fillOpacity: 0.12, interactive: false
    }).addTo(map);
  } else {
    state.brushCursor.setLatLng(event.latlng);
    state.brushCursor.setRadius(radius);
    state.brushCursor.setStyle({ color, fillColor: color });
  }
  if (state.painting) stampBrush(event.latlng);
});

map.on("mousedown", (event) => {
  if (state.mode !== "track" || !state.brush || event.originalEvent.button !== 0) return;
  state.painting = true;
  state.erasing = event.originalEvent.ctrlKey || event.originalEvent.metaKey;
  state.strokeStarted = false;
  state.skipClick = true;
  map.dragging.disable();
  stampBrush(event.latlng);
  L.DomEvent.preventDefault(event.originalEvent);
});

map.getContainer().addEventListener("wheel", (event) => {
  if (!state.brush || !event.altKey) return;
  event.preventDefault();
  event.stopPropagation();
  setBrushRadius(brushRadius() + (event.deltaY < 0 ? 2 : -2));
}, { capture: true, passive: false });

window.addEventListener("keydown", (event) => {
  if (event.key === "Alt" && state.brush) map.scrollWheelZoom.disable();
});
window.addEventListener("keyup", (event) => {
  if (event.key === "Alt") map.scrollWheelZoom.enable();
});

function stopPainting() {
  if (state.handle) {
    state.handle = null;
    state.handleOrigin = null;
    map.dragging.enable();
    if (state.mode === "reference" && state.referenceMarker) state.referenceMarker.dragging.enable();
  }
  if (!state.painting) return;
  state.painting = false;
  map.dragging.enable();
}

map.on("mouseup", stopPainting);
document.addEventListener("mouseup", stopPainting);

map.on("click", (event) => {
  if (state.mode !== "track" || state.brush || state.drag || state.skipClick) {
    state.skipClick = false;
    return;
  }
  const points = main();
  const latlng = { lat: event.latlng.lat, lon: event.latlng.lng };
  const hit = nearestPoint(event.latlng);
  if (hit) {
    state.skipClick = true;
    selectPoint(hit.segment, hit.index);
    setTimeout(() => { state.skipClick = false; }, 0);
    return;
  }
  const selected = state.selectedPoint;
  const segment = pointRefValid(selected) ? state.segments[selected.segment] : null;
  if (event.originalEvent.shiftKey && segment && selected.index === segment.length - 1) {
    remember();
    segment.push(latlng);
    state.selectedPoint = { segment: selected.segment, index: segment.length - 1 };
    state.junctionAnchor = null;
  } else if (event.originalEvent.shiftKey && segment) {
    remember();
    const origin = segment[selected.index];
    state.segments.push([origin, latlng]);
    state.selectedPoint = { segment: state.segments.length - 1, index: 1 };
    state.junctionAnchor = null;
  } else if (event.originalEvent.shiftKey) {
    remember();
    points.push(latlng);
    state.selectedPoint = { segment: 0, index: points.length - 1 };
  } else if (points.length) {
    remember();
    const index = points.length - 1;
    points[index] = latlng;
    state.selectedPoint = { segment: 0, index };
  } else {
    return;
  }
  draw();
});

document.getElementById("delete-point").onclick = () => deleteSelectedPoint();
document.getElementById("undo").onclick = () => undo();
document.getElementById("redo").onclick = () => redo();
