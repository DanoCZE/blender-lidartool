const fs = require("fs");
const path = require("path");

const root = __dirname;
const vendor = path.join(root, "vendor");

function copyFile(from, to) {
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.copyFileSync(from, to);
}

function copyDir(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const source = path.join(from, entry.name);
    const dest = path.join(to, entry.name);
    if (entry.isDirectory()) copyDir(source, dest);
    else copyFile(source, dest);
  }
}

function firstExisting(candidates) {
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error(`Soubor knihovny chybí: ${candidates.join(" | ")}`);
}

fs.rmSync(vendor, { recursive: true, force: true });
const leafletDist = path.join(root, "node_modules", "leaflet", "dist");
copyFile(path.join(leafletDist, "leaflet.js"), path.join(vendor, "leaflet", "leaflet.js"));
copyFile(path.join(leafletDist, "leaflet.css"), path.join(vendor, "leaflet", "leaflet.css"));
copyDir(path.join(leafletDist, "images"), path.join(vendor, "leaflet", "images"));
copyFile(
  firstExisting([
    path.join(root, "node_modules", "@turf", "turf", "turf.min.js"),
    path.join(root, "node_modules", "@turf", "turf", "dist", "turf.min.js"),
  ]),
  path.join(vendor, "turf.min.js"),
);
copyFile(
  firstExisting([
    path.join(root, "node_modules", "marked", "marked.min.js"),
    path.join(root, "node_modules", "marked", "lib", "marked.umd.js"),
  ]),
  path.join(vendor, "marked.min.js"),
);
console.log("vendor ready");
