const helpModal = document.getElementById("help-modal");
const helpBody = document.getElementById("help-body");
let helpReady = false;

function helpIsOpen() {
  return helpModal.classList.contains("open");
}

function slugify(text) {
  return String(text).toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

async function renderHelp() {
  if (helpReady) return;
  const response = await fetch("help.md");
  const source = await response.text();
  if (!response.ok) throw new Error("Nápovědu se nepodařilo načíst.");
  helpBody.innerHTML = typeof marked !== "undefined" && marked.parse ? marked.parse(source) : source;
  const used = new Set();
  const headings = [...helpBody.querySelectorAll("h2")];
  headings.forEach((heading) => {
    let id = slugify(heading.textContent) || "kapitola";
    let unique = id;
    let serial = 2;
    while (used.has(unique)) {
      unique = `${id}-${serial}`;
      serial += 1;
    }
    used.add(unique);
    heading.id = unique;
  });
  if (headings.length) {
    const toc = document.createElement("nav");
    toc.className = "help-toc";
    const title = document.createElement("p");
    title.textContent = "Obsah";
    const list = document.createElement("ul");
    headings.forEach((heading) => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `#${heading.id}`;
      link.textContent = heading.textContent;
      item.appendChild(link);
      list.appendChild(item);
    });
    toc.append(title, list);
    const first = helpBody.querySelector("h1");
    if (first && first.nextSibling) helpBody.insertBefore(toc, first.nextSibling);
    else helpBody.prepend(toc);
  }
  helpReady = true;
}

function openHelp() {
  renderHelp()
    .catch((error) => {
      helpBody.textContent = error.message;
    })
    .finally(() => {
      helpModal.classList.add("open");
      helpModal.setAttribute("aria-hidden", "false");
      document.getElementById("help-close").focus();
    });
}

function closeHelp() {
  helpModal.classList.remove("open");
  helpModal.setAttribute("aria-hidden", "true");
  document.getElementById("help").focus();
}

document.getElementById("help").onclick = () => openHelp();
document.getElementById("help-close").onclick = () => closeHelp();
helpModal.addEventListener("click", (event) => {
  if (event.target === helpModal) closeHelp();
});
helpBody.addEventListener("click", (event) => {
  const link = event.target.closest("a[href^='#']");
  if (!link) return;
  event.preventDefault();
  const target = helpBody.querySelector(link.getAttribute("href"));
  if (target) target.scrollIntoView({ block: "start" });
});
