// Small progressive enhancements. Kept in a file (not inline) so the Content-Security-Policy can forbid inline script.
document.addEventListener("DOMContentLoaded", () => {
  // Tabs: <button class="tab" data-tab="preview"> toggles elements with data-pane="preview".
  document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== target; });
      document.querySelectorAll(".tab[data-tab]").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
  // Confirmation prompt: <form data-confirm="Question?">
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => { if (!window.confirm(form.dataset.confirm)) e.preventDefault(); });
  });
  // Live word count for the editor.
  const editor = document.querySelector("textarea[data-wordcount]");
  const counter = document.getElementById("wordcount");
  if (editor && counter) {
    const update = () => { counter.textContent = (editor.value.match(/\S+/g) || []).length.toLocaleString() + " words"; };
    editor.addEventListener("input", update);
    update();
  }
});
