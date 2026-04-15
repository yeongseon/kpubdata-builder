document$.subscribe(() => {
  const blocks = document.querySelectorAll("pre code.language-mermaid");

  for (const block of blocks) {
    const pre = block.parentElement;

    if (!pre || pre.dataset.processed === "true") {
      continue;
    }

    const container = document.createElement("div");
    container.className = "mermaid";
    container.textContent = block.textContent ?? "";

    pre.replaceWith(container);
  }

  if (window.mermaid) {
    window.mermaid.initialize({ startOnLoad: false });
    window.mermaid.run({ querySelector: ".mermaid" });
  }
});
