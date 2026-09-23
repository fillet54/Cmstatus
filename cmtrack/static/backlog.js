/* Backlog ordering with native drag and drop (no libraries).
 *
 * After a drop, or a top/up/down click, the moved item's new neighbours are sent to the move endpoint
 * (data-move-url on #backlog-items, "__KEY__" replaced by the item's key), which gives it a rank between
 * theirs; no other item is touched. If the server refuses (e.g. someone else reordered meanwhile), a toast
 * explains and the list reloads from data-reload-url. Markup contract: ui.rank_item in components.html.
 */
(() => {
  const root = document.getElementById("backlog-items");
  if (!root || root.dataset.dnd) return;
  root.dataset.dnd = "1";
  const items = () => root.querySelectorAll("#backlog-list > li[data-key]");
  let dragged = null, startNext = null;

  function toast(message) {
    const box = document.getElementById("ui-toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "ui-alert ui-alert--danger";
    el.setAttribute("role", "alert");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), 6000);
  }

  function renumber() {
    items().forEach((li, i) => { const n = li.querySelector("[data-pos]"); if (n) n.textContent = i + 1; });
  }

  async function save(li) {
    const prev = li.previousElementSibling, next = li.nextElementSibling;
    const key = (el) => (el && el.dataset.key) || null;
    renumber();
    li.classList.add("saving");
    try {
      const url = root.dataset.moveUrl.replace("__KEY__", encodeURIComponent(li.dataset.key));
      const res = await fetch(url, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({after: key(prev), before: key(next)}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText);
      li.dataset.rank = data.rank;
    } catch (err) {
      toast(`Couldn't move ${li.dataset.key}: ${err.message}`);
      htmx.ajax("GET", root.dataset.reloadUrl, {target: "#backlog-items"});
    } finally {
      li.classList.remove("saving");
    }
  }

  root.addEventListener("dragstart", (e) => {
    const li = e.target.closest && e.target.closest("li[data-key]");
    if (!li) return;
    dragged = li;
    startNext = li.nextElementSibling;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", li.dataset.key);
    requestAnimationFrame(() => li.classList.add("dragging"));
  });

  root.addEventListener("dragover", (e) => {
    if (!dragged) return;
    e.preventDefault();
    const over = e.target.closest && e.target.closest("li[data-key]");
    if (!over || over === dragged) return;
    const box = over.getBoundingClientRect();
    const below = e.clientY > box.top + box.height / 2;
    over.parentNode.insertBefore(dragged, below ? over.nextElementSibling : over);
  });

  root.addEventListener("drop", (e) => { if (dragged) e.preventDefault(); });

  root.addEventListener("dragend", () => {
    if (!dragged) return;
    const li = dragged;
    dragged = null;
    li.classList.remove("dragging");
    if (li.nextElementSibling !== startNext) save(li);
  });

  root.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("[data-move]");
    if (!btn) return;
    const li = btn.closest("li[data-key]"), list = li.parentNode;
    const prev = li.previousElementSibling, next = li.nextElementSibling;
    if (btn.dataset.move === "top" && prev) list.prepend(li);
    else if (btn.dataset.move === "up" && prev) list.insertBefore(li, prev);
    else if (btn.dataset.move === "down" && next) list.insertBefore(next, li);
    else return;
    save(li);
    btn.focus();
  });
})();
