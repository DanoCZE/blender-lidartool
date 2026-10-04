function main() {
  if (!state.segments.length) state.segments = [[]];
  return state.segments[0];
}

function sameCoord(left, right) {
  return !!left && !!right && Math.abs(left.lat - right.lat) < 1e-8 && Math.abs(left.lon - right.lon) < 1e-8;
}

function joinLines(left, right) {
  const start = left[0];
  const end = left[left.length - 1];
  const otherStart = right[0];
  const otherEnd = right[right.length - 1];
  if (sameCoord(end, otherStart)) return left.concat(right.slice(1));
  if (sameCoord(end, otherEnd)) return left.concat(right.slice(0, -1).reverse());
  if (sameCoord(start, otherStart)) return right.slice().reverse().concat(left.slice(1));
  if (sameCoord(start, otherEnd)) return right.concat(left.slice(1));
  return null;
}

function repairMainLine() {
  if (main().length >= 2) return false;
  let lines = state.segments.filter((segment) => Array.isArray(segment) && segment.length >= 2).map((segment) => segment.slice());
  if (!lines.length) return false;
  let merged = true;
  while (merged) {
    merged = false;
    for (let left = 0; left < lines.length; left += 1) {
      let joined = null;
      let right = -1;
      for (let index = left + 1; index < lines.length; index += 1) {
        joined = joinLines(lines[left], lines[index]);
        if (joined) {
          right = index;
          break;
        }
      }
      if (!joined) continue;
      lines.splice(right, 1);
      lines.splice(left, 1);
      lines.push(joined);
      merged = true;
      break;
    }
  }
  lines.sort((left, right) => right.length - left.length);
  state.segments = lines;
  state.selectedPoint = null;
  state.junctionAnchor = null;
  return true;
}

function snapshot() {
  return JSON.stringify({
    segments: state.segments,
    brushes: state.brushes,
    reference: state.reference ? {
      lat: state.reference.lat,
      lon: state.reference.lon,
      rotation: state.reference.rotation,
      scale: state.reference.scale,
      opacity: state.reference.opacity,
      widthM: state.reference.widthM,
      width: state.reference.width,
      height: state.reference.height,
      version: state.reference.version
    } : null
  });
}

function restore(raw) {
  const saved = JSON.parse(raw);
  state.segments = saved.segments && saved.segments.length ? saved.segments : [[]];
  state.brushes = saved.brushes || [];
  if ("reference" in saved) {
    if (saved.reference && state.reference) Object.assign(state.reference, saved.reference);
    else state.reference = saved.reference;
    if (state.reference) {
      document.getElementById("reference-rotation").value = state.reference.rotation;
      document.getElementById("reference-scale").value = state.reference.scale;
      document.getElementById("reference-opacity").value = state.reference.opacity;
    }
  }
  draw();
  renderReference();
}

function remember() {
  state.history.push(snapshot());
  state.future = [];
  if (state.history.length > 80) state.history.shift();
}

function undo() {
  if (!state.history.length) return;
  state.future.push(snapshot());
  restore(state.history.pop());
}

function redo() {
  if (!state.future.length) return;
  state.history.push(snapshot());
  restore(state.future.pop());
}
