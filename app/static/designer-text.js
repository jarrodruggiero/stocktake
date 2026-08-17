/* The text designer: every word in the extracted text is a button.

   A real <button> rather than a span with a handler, because "click the value"
   is the whole interaction and a pointer-only version is unusable for anyone
   who cannot use a mouse. */
window.startDesigner({ surface: "doc", item: ".tok" });
