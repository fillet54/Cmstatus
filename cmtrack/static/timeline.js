// Timelines (ui.timeline): centre each on today when it appears (page load, htmx swap). One narrower than its box
// is fetched again at the box's width (data-refit), so a zoomed-out timeline fills the width instead of leaving
// a gap; the same happens after the window grows.
(function () {
  if (window.cmtrackTimeline) return;
  window.cmtrackTimeline = true;
  function timelines(root) {
    return root.matches && root.matches(".ui-timeline") ? [root] : Array.from(root.querySelectorAll(".ui-timeline"));
  }
  function refit(el) {
    const svg = el.querySelector("svg");
    if (!el.dataset.refit || !svg || !window.htmx || svg.width.baseVal.value >= el.clientWidth - 2) return false;
    const url = el.dataset.refit + (el.dataset.refit.includes("?") ? "&" : "?") + "width=" + el.clientWidth;
    window.htmx.ajax("GET", url, { target: el.dataset.refitTarget, swap: "outerHTML" });
    return true;
  }
  function show(root) {
    timelines(root).forEach(function (el) {
      if (!refit(el)) el.scrollLeft = Number(el.dataset.center) - el.clientWidth / 2;
    });
  }
  document.addEventListener("DOMContentLoaded", function () { show(document); });
  document.addEventListener("htmx:load", function (e) { show(e.detail.elt); });
  let resized;
  window.addEventListener("resize", function () {
    clearTimeout(resized);
    resized = setTimeout(function () { timelines(document).forEach(refit); }, 250);
  });
  if (document.readyState !== "loading") show(document);
})();
