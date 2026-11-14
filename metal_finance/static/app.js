/* Metal Finance - edycja w miejscu (checkboxy konsumentów, komórki arkusza) + drobiazgi UI. */
(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const root = document.querySelector('meta[name="script-root"]')?.content || "";

  function toast(msg) {
    const el = document.getElementById("mf-toast");
    if (!el || !window.bootstrap) { alert(msg); return; }
    el.querySelector(".toast-body").textContent = msg;
    bootstrap.Toast.getOrCreateInstance(el, { delay: 4000 }).show();
  }

  async function api(path, body) {
    const res = await fetch(root + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body),
    });
    let data;
    try { data = await res.json(); } catch { data = { ok: false, error: `Błąd serwera (${res.status})` }; }
    if (!res.ok || !data.ok) throw new Error(data.error || `Błąd (${res.status})`);
    return data;
  }

  function sel(key) { return `[data-bind="${CSS.escape(key)}"]`; }

  function apply(data) {
    for (const [key, text] of Object.entries(data.bind || {})) {
      document.querySelectorAll(sel(key)).forEach((el) => { el.textContent = text; });
    }
    for (const [key, tone] of Object.entries(data.tone || {})) {
      document.querySelectorAll(sel(key)).forEach((el) => {
        el.classList.remove("tone-pos", "tone-neg", "tone-zero");
        el.classList.add(`tone-${tone}`);
      });
    }
    for (const [pid, row] of Object.entries(data.rows || {})) {
      document.querySelectorAll(`tr[data-product="${pid}"]`).forEach((tr) => {
        tr.classList.toggle("unassigned", row.unassigned);
        tr.querySelectorAll("input.js-consumer").forEach((cb) => {
          cb.checked = row.consumers.includes(Number(cb.dataset.person));
        });
        if ("weighted" in row) {
          tr.classList.toggle("weighted", row.weighted);
          tr.querySelectorAll(".js-weighted").forEach((b) => {
            b.classList.toggle("active", row.weighted);
            b.setAttribute("aria-pressed", String(row.weighted));
          });
        }
        for (const [person, w] of Object.entries(row.weights || {})) {
          tr.querySelectorAll(`input.js-weight[data-person="${person}"]`).forEach((inp) => {
            if (document.activeElement !== inp) inp.value = w;
          });
        }
      });
    }
    document.querySelectorAll("[data-show-if-text]").forEach((box) => {
      const inner = box.querySelector("[data-bind]");
      box.classList.toggle("d-none", !inner || !inner.textContent.trim());
    });
  }

  // --- checkbox konsumenta ---------------------------------------------------------------
  document.addEventListener("change", async (e) => {
    const cb = e.target;
    if (!cb.matches("input.js-consumer")) return;
    cb.disabled = true;
    try {
      apply(await api(`/api/products/${cb.dataset.product}/consumers`, { person_id: Number(cb.dataset.person), on: cb.checked }));
    } catch (err) {
      cb.checked = !cb.checked;
      toast(err.message);
    } finally {
      cb.disabled = false;
    }
  });

  // --- komórki: nazwa / ilość / kwota ----------------------------------------------------------
  document.addEventListener("focusin", (e) => {
    if (e.target.matches(".js-field")) e.target.dataset.orig = e.target.value;
  });
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (!t.matches(".js-field")) return;
    if (e.key === "Enter") { e.preventDefault(); t.blur(); }
    if (e.key === "Escape") { t.value = t.dataset.orig ?? t.value; t.blur(); }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (!t.matches(".js-field")) return;
    const field = t.dataset.field;
    t.classList.remove("saved", "error");
    t.classList.add("saving");
    try {
      const data = await api(`/api/products/${t.dataset.product}`, { [field]: t.value });
      apply(data);
      if (data.values && field in data.values) t.value = data.values[field];
      t.dataset.orig = t.value;
      t.classList.add("saved");
      setTimeout(() => t.classList.remove("saved"), 900);
    } catch (err) {
      t.classList.add("error");
      toast(err.message);
    } finally {
      t.classList.remove("saving");
    }
  });

  // --- wagi osób w pozycji --------------------------------------------------------------------
  document.addEventListener("focusin", (e) => {
    if (e.target.matches("input.js-weight")) { e.target.dataset.orig = e.target.value; e.target.select(); }
  });
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (!t.matches("input.js-weight")) return;
    if (e.key === "Enter") { e.preventDefault(); t.blur(); }
    if (e.key === "Escape") { t.value = t.dataset.orig ?? t.value; t.blur(); }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (!t.matches("input.js-weight")) return;
    t.classList.remove("saved", "error");
    try {
      const data = await api(`/api/products/${t.dataset.product}/weights`, { person_id: Number(t.dataset.person), weight: t.value });
      apply(data);
      t.value = data.rows?.[t.dataset.product]?.weights?.[t.dataset.person] ?? t.value;
      t.dataset.orig = t.value;
      t.classList.add("saved");
      setTimeout(() => t.classList.remove("saved"), 900);
    } catch (err) {
      t.classList.add("error");
      toast(err.message);
    }
  });

  // --- przyciski "wszyscy" / "nikt" / "wagi" ----------------------------------------------------
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-consumers-set]");
    if (btn) {
      btn.disabled = true;
      try {
        apply(await api(`/api/products/${btn.dataset.product}/consumers`, { set: btn.dataset.consumersSet }));
      } catch (err) { toast(err.message); } finally { btn.disabled = false; }
      return;
    }
    const wbtn = e.target.closest(".js-weighted");
    if (wbtn) {
      wbtn.disabled = true;
      try {
        apply(await api(`/api/products/${wbtn.dataset.product}`, { weighted: !wbtn.classList.contains("active") }));
      } catch (err) { toast(err.message); } finally { wbtn.disabled = false; }
      return;
    }
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      const src = document.querySelector(copy.dataset.copy);
      const text = src?.value ?? src?.textContent ?? "";
      (navigator.clipboard?.writeText(text) ?? Promise.reject()).then(
        () => { copy.innerHTML = '<i class="bi bi-clipboard-check"></i> Skopiowano'; },
        () => { src.select?.(); document.execCommand("copy"); },
      );
      return;
    }
    const emoji = e.target.closest("[data-emoji]");
    if (emoji) {
      const picker = emoji.closest(".emoji-picker");
      const input = picker?.closest("form")?.querySelector(picker.dataset.target);
      if (input) input.value = emoji.dataset.emoji;
    }
  });

  // --- formularze: potwierdzenie + blokada podwójnego wysłania ---------------------------
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    if (form.dataset.busy) {
      const btn = form.querySelector('[type="submit"], button:not([type])');
      if (btn) {
        setTimeout(() => {  // po wysłaniu - inaczej przeglądarka nie wyśle formularza
          btn.disabled = true;
          btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span> ${form.dataset.busy}`;
        }, 0);
      }
    }
  });

  // --- pole zdjęć (dropzone) ---------------------------------------------------------------------
  document.querySelectorAll(".dropzone").forEach((zone) => {
    const input = document.getElementById(zone.getAttribute("for"));
    const out = zone.querySelector(".dz-files");
    const show = () => {
      const n = input.files.length;
      out.textContent = n ? `Wybrano: ${[...input.files].map((f) => f.name).join(", ")}` : "";
    };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", (e) => { input.files = e.dataTransfer.files; show(); });
  });

  if (location.hash === "#add-product") {
    document.querySelector('#add-product input[name="name"]')?.focus();
  }
})();
