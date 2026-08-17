/* Drag to reorder columns — an enhancement over the ↑/↓ buttons, never a
   replacement for them.

   The buttons are the real mechanism: they are submit buttons, so they work
   with no JavaScript, they are reachable by keyboard, and a screen reader
   announces them. Drag is faster with a mouse and unusable without one, which
   is exactly the wrong way round to build the only way of doing something.

   This file does one thing: reorder the <li>s and rewrite the hidden inputs
   they carry. Submitting is still the form's job, and the server sees the same
   POST either way, so there is no second code path to keep honest.

   No library. The HTML drag-and-drop API is unpleasant but small, and pulling
   in a sortable-list package for forty lines would cost more than it saves —
   the memory budget is a real constraint here and the CSP forbids a
   CDN anyway. */
(function () {
  const form = document.getElementById("colorder");
  const list = document.getElementById("colorderlist");
  if (!form || !list) return;

  /* Only claim to be draggable where it will actually work. Touch devices fire
     no drag events, so the grip would be a handle that does nothing. */
  if (!("draggable" in document.createElement("div"))) return;
  form.classList.add("candrag");

  let dragged = null;

  const clear = () => list.querySelectorAll("li").forEach((li) => {
    li.classList.remove("dragging", "dropbefore");
  });

  list.addEventListener("dragstart", (e) => {
    dragged = e.target.closest("li");
    if (!dragged) return;
    dragged.classList.add("dragging");
    /* Firefox ignores a drag that sets no data. */
    e.dataTransfer.setData("text/plain", dragged.dataset.key || "");
    e.dataTransfer.effectAllowed = "move";
  });

  list.addEventListener("dragover", (e) => {
    const over = e.target.closest("li");
    if (!dragged || !over || over === dragged) return;
    e.preventDefault();
    list.querySelectorAll("li").forEach((li) => li.classList.remove("dropbefore"));
    over.classList.add("dropbefore");
  });

  list.addEventListener("drop", (e) => {
    const over = e.target.closest("li");
    if (!dragged || !over || over === dragged) return;
    e.preventDefault();
    /* Before or after, decided by which side of the midpoint it was dropped on
       — otherwise the last position in the list is unreachable. */
    const box = over.getBoundingClientRect();
    const after = e.clientX > box.left + box.width / 2;
    over.parentNode.insertBefore(dragged, after ? over.nextSibling : over);
    clear();
    dragged = null;
    renumber();
  });

  list.addEventListener("dragend", () => { clear(); dragged = null; });

  /* The hidden inputs are what the server reads; the visible order is just
     pixels. Rewriting them from the DOM after every move keeps the two the
     same thing rather than two things that agree most of the time. */
  function renumber() {
    list.querySelectorAll("li").forEach((li, index) => {
      const input = li.querySelector('input[name="order"]');
      if (input) input.value = li.dataset.key;
      /* The end buttons are the ones that cannot do anything. */
      const [up, down] = li.querySelectorAll("button");
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === list.children.length - 1;
    });
  }
})();
