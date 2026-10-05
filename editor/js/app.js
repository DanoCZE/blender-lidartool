const openStep = debugStart("Otevřít editor");
fetch(`/api/session?token=${encodeURIComponent(token)}`)
  .then((response) => response.json().then((data) => ({ ok: response.ok, data })))
  .then(({ ok, data }) => {
    if (!ok) throw new Error(data.detail || "Editor se nepodařilo otevřít.");
    debugEnd(openStep);
    state.segments = data.segments && data.segments.length ? data.segments : [[]];
    state.brushes = data.zone1_brush || [];
    if (data.profile) document.getElementById("profile").value = data.profile;
    if (data.reference_ready && data.reference) restoreReference(data.reference);
    else if (data.reference_ready) placeReference(map.getCenter());
    fillZones(data.zones || [], data.terrain_buffer_m || 2000, data.road_buffer_m || 25);
    if (typeof restoreBuildingSelection === "function") restoreBuildingSelection(data.buildings || []);
    applyBrushChrome();
    try {
      draw();
    } finally {
      drawBrushes();
    }
    fitWhenReady();
    setTimeout(() => drawBrushes(), 300);
  })
  .catch((error) => {
    setStatus(error.message);
    debugEnd(openStep, error);
  });
