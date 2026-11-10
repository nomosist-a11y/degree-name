// エディター: photos of the real gear a block is based on (display only).
//
// GET /api/editor/photo?name=&based_on=&plugin=&fetch=  ->  {kind, origin, query, links,
//   note, photo: {source: "user"|"commons", url, author, license, license_url, page, title},
//   commons: "pending"|"none"|"off"|"error"}
// POST /api/editor/photo {name, data}  registers the player's own picture.
//
// The photo card sits beside the parameter panel (above it, folded, on phones); the
// sidebar's device list has a preview pane for the hovered / focused device so the
// player can compare before replacing. Search buttons open Google Images / Reverb in
// the browser; their images are never fetched by the server.
"use strict";

const EdPhotos = (() => {
  const MOBILE = window.matchMedia("(max-width: 899px)");
  const cache = new Map();          // key -> Promise<result>  (final results only)
  const DWELL_MS = 350;             // hover this long before searching Commons

  const keyOf = (name, basedOn) => `${name}\u0000${basedOn || ""}`;

  function query(name, basedOn, plugin, fetchIt) {
    const p = new URLSearchParams({ name, based_on: basedOn || "", plugin: plugin ? "1" : "0",
      fetch: fetchIt ? "1" : "0" });
    return fetch(`/api/editor/photo?${p}`).then(async r => {
      const data = await r.json().catch(() => null);
      if (!r.ok || !data) throw new Error((data && data.error) || `写真の情報を読めませんでした(${r.status})`);
      return data;
    });
  }

  // Cached answer first (cheap); search Commons only when asked to.
  function lookup(name, basedOn, plugin, { fetchIt = true } = {}) {
    const key = keyOf(name, basedOn);
    if (cache.has(key)) return cache.get(key);
    const pr = query(name, basedOn, plugin, false).then(r => {
      if (r.commons === "pending" && fetchIt) return query(name, basedOn, plugin, true);
      return r;
    });
    const settled = pr.then(r => {
      if (["pending", "error", "limited"].includes(r.commons)) cache.delete(key);
      return r;
    }, err => { cache.delete(key); throw err; });
    cache.set(key, settled);
    return settled;
  }

  function forget(name) {
    for (const k of [...cache.keys()]) if (k.split("\u0000")[0] === name) cache.delete(k);
  }

  function isPlugin(modelId) {
    const hit = typeof ED !== "undefined" && ED.catIndex ? ED.catIndex.get(modelId) : null;
    return !!(hit && hit.model && hit.model.plugin);
  }

  // ------------------------------------------------------------ pieces

  function kindText(r) {
    if (r.kind === "original") return "Neural DSP オリジナル";
    if (r.kind === "plugin") return "Neural DSP プラグインのデバイス";
    if (r.kind === "unknown") return "元ネタの情報がありません";
    return r.origin;
  }

  function credit(photo) {
    if (photo.source === "user") return el("div", { class: "ph-credit" }, "あなたの写真");
    const lic = photo.license_url
      ? el("a", { href: photo.license_url, target: "_blank", rel: "noopener" }, photo.license)
      : photo.license;
    return el("div", { class: "ph-credit" },
      "写真: ", el("span", { class: "ph-author" }, photo.author || "不明"), " / ", lic, " / ",
      photo.page ? el("a", { href: photo.page, target: "_blank", rel: "noopener" }, "Wikimedia Commons")
        : "Wikimedia Commons");
  }

  function searchButtons(r) {
    if (!r.links) return null;
    return el("div", { class: "ph-links" },
      el("a", { class: "ph-btn", href: r.links.images, target: "_blank", rel: "noopener",
        title: `「${r.query}」をブラウザで画像検索` }, "画像検索"),
      el("a", { class: "ph-btn", href: r.links.reverb, target: "_blank", rel: "noopener",
        title: `「${r.query}」を Reverb で探す` }, "Reverb"));
  }

  function zoom(photo, r) {
    const box = el("div", { class: "ph-zoom", role: "dialog", "aria-label": `${r.origin || r.name} の写真` },
      el("figure", {},
        el("img", { src: photo.url, alt: r.origin || r.name }),
        el("figcaption", {}, el("b", {}, r.origin || r.name), credit(photo))));
    const close = () => { box.remove(); document.removeEventListener("keydown", onKey, true); };
    const onKey = e => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    box.addEventListener("click", e => { if (e.target === box || e.target.tagName === "IMG") close(); });
    document.addEventListener("keydown", onKey, true);
    document.body.append(box);
  }

  function readFile(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = () => reject(new Error("画像を読めませんでした"));
      fr.readAsDataURL(file);
    });
  }

  async function register(name, file, had, onDone) {
    if (!file || !/^image\/(jpeg|png|webp|gif)$/.test(file.type)) {
      toast("JPEG / PNG / WebP / GIF の画像を選んでください", true);
      return;
    }
    if (file.size > 12 * 1024 * 1024) { toast("画像が大きすぎます(12 MB まで)", true); return; }
    const text = had
      ? `「${name}」の写真を、この画像に入れ替えますか?\n前の写真は photos/old/ に移します(消しません)。`
      : `この画像を「${name}」の写真として登録しますか?\n~/.qctone/photos/ に保存します。本体には何も書きません。`;
    if (!window.confirm(text)) return;
    try {
      const data = await readFile(file);
      await api("/api/editor/photo", { method: "POST", body: { name, data } });
      forget(name);
      toast(`「${name}」の写真を登録しました`);
      onDone();
    } catch (e) { /* api() already showed it */ }
  }

  function dropTarget(node, name, hasPhoto, onDone) {
    node.addEventListener("dragover", e => {
      if ([...(e.dataTransfer.types || [])].includes("Files")) { e.preventDefault(); node.classList.add("drop"); }
    });
    node.addEventListener("dragleave", () => node.classList.remove("drop"));
    node.addEventListener("drop", e => {
      e.preventDefault();
      node.classList.remove("drop");
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      register(name, file, hasPhoto, onDone);
    });
  }

  function pickButton(name, hasPhoto, onDone) {
    const input = el("input", { type: "file", accept: "image/jpeg,image/png,image/webp,image/gif", hidden: true });
    input.addEventListener("change", () => { register(name, input.files[0], hasPhoto, onDone); input.value = ""; });
    const btn = el("button", { type: "button", class: "ph-btn ghost",
      title: "自分で撮った写真などを登録(写真をここへドラッグしても登録できます)" },
    hasPhoto ? "写真を変える…" : "写真を登録…");
    btn.addEventListener("click", () => input.click());
    return [btn, input];
  }

  // The picture area: photo, or a placeholder saying why there is none.
  function picture(r, { compact = false } = {}) {
    if (r.photo) {
      const img = el("img", { src: r.photo.url, alt: r.origin || r.name, loading: "lazy", decoding: "async" });
      const fig = el("button", { type: "button", class: "ph-img", title: "クリックで大きく表示",
        "aria-label": `${r.origin || r.name} の写真を大きく表示` }, img);
      fig.addEventListener("click", () => zoom(r.photo, r));
      return compact ? el("div", { class: "ph-img" }, img) : fig;
    }
    let why = "写真なし";
    if (r.kind === "hardware") {
      why = r.commons === "pending" ? "写真を探しています…"
        : r.commons === "off" ? "写真なし(Commons の検索は切ってあります)"
        : r.commons === "limited" ? "Commons から「少し待って」と言われています。しばらくしてから選び直してください"
        : r.commons === "error" ? "写真なし(Commons に接続できませんでした)"
        : "自由なライセンスの写真が見つかりませんでした";
    } else if (r.kind === "original") {
      why = "実機のモデリングではありません";
    }
    return el("div", { class: "ph-img none" }, el("span", {}, why));
  }

  // ------------------------------------------------------------ the card beside the parameters

  let cardSeq = 0;
  const openState = { mobile: false, desktop: true };   // kept across re-renders

  function card(b) {
    const name = b.title || b.model;
    const seq = ++cardSeq;
    const det = el("details", { class: "ph", "data-c": b.color || "util" });
    det.open = MOBILE.matches ? openState.mobile : openState.desktop;
    det.addEventListener("toggle", () => { openState[MOBILE.matches ? "mobile" : "desktop"] = det.open; });
    const sumText = el("span", { class: "ph-sum-text" }, "実機");
    const sumThumb = el("span", { class: "ph-sum-thumb", "aria-hidden": "true" });
    const summary = el("summary", { title: "実機の写真と元ネタ" },
      el("span", { class: "ph-label" }, "実機"), sumThumb, sumText);
    const body = el("div", { class: "ph-body" }, el("div", { class: "ph-img none" }, el("span", {}, "…")));
    det.append(summary, body);
    const draw = () => {
      lookup(name, b.based_on, isPlugin(b.model_id)).then(r => {
        if (seq !== cardSeq) return;
        sumText.textContent = kindText(r);
        sumThumb.replaceChildren(r.photo ? el("img", { src: r.photo.url, alt: "" }) : "");
        const [pick, input] = r.kind === "hardware" || r.photo ? pickButton(name, !!r.photo, redraw) : [null, null];
        body.replaceChildren(
          picture(r),
          el("div", { class: "ph-info" },
            el("div", { class: "ph-origin" }, kindText(r)),
            r.kind === "hardware" ? el("div", { class: "ph-src" },
              `${name} の元ネタ(${r.source === "modelrepo" ? "本体のモデル情報" : "デバイス一覧表"})`) : null,
            r.note ? el("div", { class: "ph-src" }, r.note) : null,
            r.photo ? credit(r.photo) : null,
            el("div", { class: "ph-actions" }, searchButtons(r), pick, input)));
      }).catch(err => {
        body.replaceChildren(el("div", { class: "ph-img none" }, el("span", {}, err.message)));
      });
    };
    const redraw = () => { forget(name); draw(); };
    dropTarget(det, name, false, redraw);
    draw();
    return det;
  }

  // Parameter panel + photo card, side by side (stacked on phones).
  function wrap(ccNode, b) {
    if (!b) return ccNode;
    return el("div", { class: "ed-pp" }, card(b), ccNode);
  }

  // ------------------------------------------------------------ sidebar preview

  let pvTimer = null;
  let pvSeq = 0;
  let pvPinned = null;      // the device clicked / focused last (shown when the mouse leaves)

  function previewPane() {
    return el("div", { class: "sb-pv", "aria-live": "polite" },
      el("div", { class: "sb-pv-empty" }, "デバイスにマウスを乗せる(スマホはタップ)と実機の写真が出ます"));
  }

  function showPreview(pane, m) {
    if (!pane) return;
    clearTimeout(pvTimer);
    const seq = ++pvSeq;
    const draw = r => {
      if (seq !== pvSeq) return;
      pane.replaceChildren(
        picture(r, { compact: true }),
        el("div", { class: "sb-pv-text" },
          el("b", {}, m.name),
          el("span", {}, kindText(r)),
          r.photo ? credit(r.photo) : searchButtons(r)));
      pane.dataset.c = m.color || "util";
    };
    // From the cache at once; search Commons only if the pointer stays a moment.
    query(m.name, m.based_on, m.plugin, false).then(r => {
      draw(r);
      if (r.commons === "pending") {
        pvTimer = setTimeout(() => {
          if (seq !== pvSeq) return;
          lookup(m.name, m.based_on, m.plugin).then(draw).catch(() => {});
        }, DWELL_MS);
      }
    }).catch(() => {});
  }

  function hookItem(item, m, pane) {
    item.addEventListener("mouseenter", () => showPreview(pane, m));
    item.addEventListener("focus", () => { pvPinned = m; showPreview(pane, m); });
    item.addEventListener("click", () => { pvPinned = m; showPreview(pane, m); });
  }

  function hookList(items, pane) {
    items.addEventListener("mouseleave", () => {
      clearTimeout(pvTimer);
      if (pvPinned) showPreview(pane, pvPinned);
    });
  }

  MOBILE.addEventListener("change", () => { if (typeof renderParams === "function") renderParams(); });

  return { wrap, previewPane, hookItem, hookList, lookup, forget };
})();
window.EdPhotos = EdPhotos;
