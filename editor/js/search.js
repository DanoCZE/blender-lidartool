const results = document.getElementById("results");
const queryInput = document.getElementById("query");
let addressTimer = 0;
let searchSerial = 0;
let addressMarker = null;

function queryValue() {
  return String(queryInput.value || "").trim();
}

function goToAddress(item) {
  results.classList.add("hidden");
  queryInput.value = item.label;
  map.setView([item.lat, item.lon], 17);
  if (addressMarker) map.removeLayer(addressMarker);
  addressMarker = L.marker([item.lat, item.lon]).addTo(map);
  addressMarker.bindPopup(item.label).openPopup();
}

function showResults(items) {
  results.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.label;
    button.onclick = () => goToAddress(item);
    row.appendChild(button);
    results.appendChild(row);
  });
  results.classList.toggle("hidden", items.length === 0);
}

async function searchAddress(query, jumpToFirst) {
  const serial = ++searchSerial;
  const response = await fetch(`/api/geocode?q=${encodeURIComponent(query)}`);
  const data = await response.json().catch(() => ({}));
  if (serial !== searchSerial) return;
  if (!response.ok) throw new Error(data.detail || "Adresa se nenašla.");
  const items = data.results || [];
  showResults(items);
  if (jumpToFirst && items.length === 1) goToAddress(items[0]);
}

function runAddressSearch(jumpToFirst) {
  const query = queryValue();
  if (query.length < (jumpToFirst ? 2 : 3)) {
    if (!jumpToFirst) {
      searchSerial += 1;
      showResults([]);
    }
    return;
  }
  if (jumpToFirst) {
    const step = debugStart("Hledat adresu");
    searchAddress(query, true).then(() => debugEnd(step)).catch((error) => {
      showResults([]);
      setStatus(error.message);
      debugEnd(step, error);
    });
    return;
  }
  searchAddress(query, false).catch(() => showResults([]));
}

document.getElementById("search").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(addressTimer);
  runAddressSearch(true);
});

queryInput.addEventListener("input", () => {
  clearTimeout(addressTimer);
  const query = queryValue();
  if (query.length < 3) {
    searchSerial += 1;
    showResults([]);
    return;
  }
  addressTimer = setTimeout(() => runAddressSearch(false), 400);
});

queryInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  clearTimeout(addressTimer);
  runAddressSearch(true);
}, true);

document.addEventListener("click", (event) => {
  if (!event.target.closest("#search")) results.classList.add("hidden");
});
