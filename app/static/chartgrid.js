/* Drag charts around the page. Cards snap to the grid — there is no free
 * placement, so a page can be reordered but not made crooked.
 *
 * Reordering is a list operation: dropping card A on card B moves A to B's
 * index and CSS grid re-flows everything. The new order is posted once, on
 * drop, rather than on every hover.
 */
(function () {
  const grid = document.getElementById("chartgrid");
  if (!grid) return;

  let dragging = null;

  const cards = () => [...grid.querySelectorAll(".chartcard")];

  function save() {
    fetch("/charts/order", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({ order: cards().map((c) => c.dataset.chart) }),
    }).catch(() => {
      /* Order is cosmetic: a failed save just means the next load shows the
         previous arrangement, which is better than blocking the page. */
    });
  }

  /* Move the cards, then animate them from where they were.
   *
   * Grid reordering is a DOM move, and a DOM move is instant: every card that
   * shifted a cell simply appears somewhere else, which is the "cards snap"
   * problem. FLIP fixes it without the layout ever being fake — measure First,
   * mutate (Last), Invert each card to its old position with a transform, then
   * Play back to zero. Layout is always the real one; only the paint lags.
   *
   * The dragged card is left out: it is under the cursor, and animating it
   * would fight the drag image the browser is already drawing.
   *
   * `element.animate` rather than a CSS transition because it replaces itself
   * cleanly — dragover fires continuously, so a half-finished move is the
   * normal case rather than the exception. */
  const still = window.matchMedia("(prefers-reduced-motion: reduce)");

  function reflow(mutate) {
    if (still.matches || typeof Element.prototype.animate !== "function") {
      mutate();
      return;
    }
    const before = new Map(cards().map((c) => [c, c.getBoundingClientRect()]));
    mutate();
    cards().forEach((card) => {
      const first = before.get(card);
      if (!first || card === dragging) return;
      const last = card.getBoundingClientRect();
      const dx = first.left - last.left;
      const dy = first.top - last.top;
      if (!dx && !dy) return;
      card.animate(
        [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
        { duration: 220, easing: "cubic-bezier(0.2, 0, 0, 1)" }
      );
    });
  }

  cards().forEach((card) => {
    const handle = card.querySelector(".draghandle");
    if (!handle) return;

    // Only the handle arms dragging, so charts and links stay clickable.
    handle.addEventListener("mousedown", () => (card.draggable = true));
    handle.addEventListener("touchstart", () => (card.draggable = true), { passive: true });
    card.addEventListener("dragend", () => {
      card.draggable = false;
      card.classList.remove("dragging");
      grid.classList.remove("arranging");
      dragging = null;
      save();
    });

    card.addEventListener("dragstart", (e) => {
      dragging = card;
      card.classList.add("dragging");
      grid.classList.add("arranging");
      e.dataTransfer.effectAllowed = "move";
      // Firefox needs data set for a drag to start at all.
      e.dataTransfer.setData("text/plain", card.dataset.chart);
    });

    card.addEventListener("dragover", (e) => {
      if (!dragging || dragging === card) return;
      e.preventDefault();
      const list = cards();
      const from = list.indexOf(dragging);
      const to = list.indexOf(card);
      if (from < 0 || to < 0) return;
      // Insert before or after depending on travel direction, so a card
      // dragged rightwards lands after the one it was dropped on.
      reflow(() => grid.insertBefore(dragging, from < to ? card.nextSibling : card));
    });

    card.addEventListener("drop", (e) => e.preventDefault());
  });
})();
