// Fuzzy picker: a <select data-picker> gets a search box. Type any part of an option, in order but with gaps
// allowed ("q4b2" finds "2026.Q4-b2"); arrows move, Enter picks, Escape gives up. The <select> stays as the form
// field (hidden) and gets the change event, so forms and htmx work as they do without the picker.
// Inside a .ui-pick, the search box stays hidden behind the .ui-pick__show summary until its [data-pick-edit]
// button is pressed, and the summary comes back when the box is left without picking.
(function () {
  if (window.cmtrackPicker) return;
  window.cmtrackPicker = true;
  let count = 0;

  // Lower is better; -1 = no match. A contiguous match beats a spread-out one, earlier beats later.
  function score(text, query) {
    text = text.toLowerCase();
    query = query.toLowerCase().replace(/\s+/g, "");
    if (!query) return 0;
    const at = text.indexOf(query);
    if (at >= 0) return at;
    let from = 0, last = -1, gaps = 0;
    for (const ch of query) {
      const j = text.indexOf(ch, from);
      if (j < 0) return -1;
      if (last >= 0) gaps += j - last - 1;
      last = j;
      from = j + 1;
    }
    return 100 + gaps;
  }

  function enhance(select) {
    if (select.dataset.pickerReady) return;
    select.dataset.pickerReady = "1";
    const listId = "picker-" + ++count;
    const box = document.createElement("div");
    const input = document.createElement("input");
    const list = document.createElement("ul");
    box.className = "ui-picker";
    input.className = "ui-input ui-input--mono";
    input.type = "text";
    input.autocomplete = "off";
    input.id = select.id;                      // the field's <label for> now names the search box
    select.id += "-value";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-controls", listId);
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");
    list.className = "ui-picker__list";
    list.id = listId;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    select.hidden = true;
    select.after(box);
    box.append(input, list);
    const pick_ = select.closest(".ui-pick");
    const show = pick_ && pick_.querySelector(".ui-pick__show");
    if (show) {
      box.hidden = true;
      show.querySelector("[data-pick-edit]").addEventListener("click", () => {
        show.hidden = true;
        box.hidden = false;
        input.focus();
      });
    }

    const current = () => (select.options[select.selectedIndex] || {}).text || "";
    let items = [], active = -1, typed = false;
    input.value = current();

    function render() {
      const query = typed ? input.value : "";
      items = Array.from(select.options)
        .map(o => ({ o, group: o.parentElement.tagName === "OPTGROUP" ? o.parentElement.label : "", s: score(o.text, query) }))
        .filter(x => x.s >= 0);
      if (query) items.sort((a, b) => a.s - b.s);
      items = items.slice(0, 80);
      list.innerHTML = "";
      let group = null;
      items.forEach((x, i) => {
        if (!query && x.group && x.group !== group) {
          const head = document.createElement("li");
          head.className = "ui-picker__group";
          head.setAttribute("role", "presentation");
          head.textContent = group = x.group;
          list.append(head);
        }
        const li = document.createElement("li");
        li.id = `${listId}-${i}`;
        li.setAttribute("role", "option");
        li.textContent = x.o.text;
        li.addEventListener("mousedown", e => { e.preventDefault(); pick(i); });
        list.append(li);
      });
      if (!items.length) {
        const none = document.createElement("li");
        none.className = "ui-picker__none";
        none.textContent = "No match";
        list.append(none);
      }
      active = items.length ? Math.max(0, items.findIndex(x => x.o.selected && !query)) : -1;
      mark();
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function mark() {
      list.querySelectorAll("[role=option]").forEach((li, i) => li.setAttribute("aria-selected", i === active));
      const li = document.getElementById(`${listId}-${active}`);
      if (li) {
        input.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      typed = false;
      input.value = current();
      if (show) {
        box.hidden = true;
        show.hidden = false;
      }
    }

    function pick(i) {
      const x = items[i];
      if (!x) return;
      const changed = select.value !== x.o.value;
      select.value = x.o.value;
      close();
      if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    input.addEventListener("focus", () => { input.select(); render(); });
    input.addEventListener("input", () => { typed = true; render(); });
    input.addEventListener("blur", close);
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (list.hidden) return render();
        active = Math.min(items.length - 1, Math.max(0, active + (e.key === "ArrowDown" ? 1 : -1)));
        mark();
      } else if (e.key === "Enter") {
        e.preventDefault();                    // never submit the form from the search box
        if (!list.hidden) pick(active);
      } else if (e.key === "Escape" && !list.hidden) {
        e.preventDefault();
        close();
      }
    });
  }

  function scan(root) {
    (root.matches && root.matches("select[data-picker]") ? [root] : Array.from(root.querySelectorAll("select[data-picker]")))
      .forEach(enhance);
  }
  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.addEventListener("htmx:load", e => scan(e.detail.elt));
  if (document.readyState !== "loading") scan(document);
})();
