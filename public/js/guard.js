// The one guarded pattern for anything that changes the lab (#76).
//
// There used to be three: a button that armed itself and executed on a second
// tap in the same spot (so a double tap executed, and "CONFIRM?" replaced the
// label, so the armed button no longer said which guest), native confirm()
// (which the artifact viewer and some browsers suppress, and then the action
// either never runs or always does), and Setup's own copy of the first.
//
// What this guarantees, and why each part is here:
//  - The question names the action AND its target in words — "STOP docker
//    (101)?" — because the thing being confirmed is the pair, and a row of
//    identical red buttons is where the wrong one gets pressed.
//  - The popover never overlaps the button that opened it (nor anything the
//    caller says to avoid, e.g. the menu an item was picked from), and CONFIRM
//    ignores clicks for ARM_MS after it appears. A double tap lands on the
//    trigger, which is a no-op while open, or on nothing.
//  - Focus moves to CANCEL, never CONFIRM, so a held or repeated Enter backs
//    out. Tab stays inside, Escape and CANCEL dismiss and return focus.
//
// One popover at a time: opening a second cancels the first.

import { el } from "./util.js";

const ARM_MS = 400;
const GAP = 8;

let current = null;   // { anchor, pop, close(result), place(), promise }
let seq = 0;

function intersects(a, b) {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

// What the owner can actually see, in the coordinates fixed positioning uses.
// On a phone the layout viewport can be larger than the screen (a page a few
// px too wide gets zoomed out), and a popover clamped to innerHeight then
// opens below the glass.
function viewport() {
  const v = window.visualViewport;
  const r = v
    ? { left: v.offsetLeft, top: v.offsetTop, right: v.offsetLeft + v.width, bottom: v.offsetTop + v.height }
    : { left: 0, top: 0, right: document.documentElement.clientWidth, bottom: window.innerHeight };
  r.margin = r.right - r.left < 860 ? 16 : 10;
  return r;
}

// right-aligned with the anchor (row actions sit at the right edge), clamped
// inside the page gutter
function leftFor(anchor, pop) {
  const vp = viewport();
  const w = pop.offsetWidth;
  return Math.max(vp.left + vp.margin,
    Math.min(anchor.getBoundingClientRect().right - w, vp.right - w - vp.margin));
}

// Fixed positioning against the anchor's viewport rect, so a scrolling table
// (or the page) carries the anchor and the popover follows it.
//
// The rects that matter for a double tap are where the taps landed: the
// trigger as it was when tapped, and anything in `avoid` (the menu an item
// was picked from). Those are screen positions, so they are captured at open
// and never shifted. The offset from the anchor is chosen once; re-placement
// keeps it, so the popover rides along with a scroll.
function placer(anchor, pop, avoid) {
  const tapped = [anchor.getBoundingClientRect(), ...avoid];
  let dy = null;

  const choose = () => {
    const vp = viewport();
    const a = anchor.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    const left = leftFor(anchor, pop);
    const blocks = [a, ...tapped];
    const candidates = [
      a.bottom + GAP, a.top - GAP - h,                       // beside the trigger
      Math.max(...blocks.map(b => b.bottom)) + GAP,           // clear of every tap
      Math.min(...blocks.map(b => b.top)) - GAP - h,
    ];
    const top = candidates.find(t => t >= vp.top + vp.margin && t + h <= vp.bottom - vp.margin
      && !blocks.some(b => intersects(b, { left, right: left + w, top: t, bottom: t + h })));
    return top === undefined ? null : top - a.top;
  };

  return function place() {
    if (dy === null) {
      dy = choose();
      if (dy === null) {
        // pinned to a viewport edge with no clear room: bring the trigger to
        // the middle and try again — the tapped rects stay where the taps were
        anchor.scrollIntoView({ block: "center", inline: "nearest" });
        dy = choose();
      }
      // still nothing clear: below the trigger, and the arming delay covers it
      if (dy === null) dy = anchor.getBoundingClientRect().height + GAP;
      pop.dataset.side = dy < 0 ? "above" : "below";
    }
    pop.style.left = `${Math.round(leftFor(anchor, pop))}px`;
    pop.style.top = `${Math.round(anchor.getBoundingClientRect().top + dy)}px`;
  };
}

// Shared open/close plumbing for both the confirm and the overflow menu.
function openPopover(anchor, pop, { avoid = [], initialFocus, onKey, ariaPopup }) {
  if (current) current.close(false, { restoreFocus: false });

  let resolve;
  const promise = new Promise(r => { resolve = r; });
  const place = placer(anchor, pop, avoid);

  pop.style.left = "-9999px";
  pop.style.top = "0px";
  document.body.append(pop);
  place();

  anchor.setAttribute("aria-haspopup", ariaPopup);
  anchor.setAttribute("aria-expanded", "true");

  let raf = 0;
  const reflow = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      if (!anchor.isConnected) { state.close(false, { restoreFocus: false }); return; }
      const r = anchor.getBoundingClientRect(), vp = viewport();
      // the trigger scrolled out of sight: a dialog pinned to nothing is worse
      // than none, so back out
      if (r.bottom < vp.top || r.top > vp.bottom) { state.close(false, { restoreFocus: false }); return; }
      place();
    });
  };
  const outside = (e) => {
    if (pop.contains(e.target) || anchor.contains(e.target)) return;
    state.close(false, { restoreFocus: false });
  };
  // On the document, not the popover: a re-tap on the trigger takes focus out
  // of the popover, and Escape must still dismiss it.
  const keys = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); state.close(false); return; }
    if (!pop.contains(document.activeElement) && document.activeElement !== anchor) return;
    if (e.key === "Tab") {
      const f = [...pop.querySelectorAll("button:not([disabled])")];
      if (!f.length) return;
      const i = f.indexOf(document.activeElement);
      e.preventDefault();
      f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
      return;
    }
    onKey?.(e);
  };

  const state = {
    anchor, pop, promise,
    close(result, { restoreFocus = true } = {}) {
      if (current !== state) return;
      current = null;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", reflow);
      window.removeEventListener("scroll", reflow, true);
      window.visualViewport?.removeEventListener("resize", reflow);
      window.visualViewport?.removeEventListener("scroll", reflow);
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", keys, true);
      anchor.setAttribute("aria-expanded", "false");
      pop.remove();
      if (restoreFocus && anchor.isConnected) anchor.focus();
      resolve(result);
    },
  };
  current = state;

  window.addEventListener("resize", reflow);
  window.addEventListener("scroll", reflow, true);
  window.visualViewport?.addEventListener("resize", reflow);
  window.visualViewport?.addEventListener("scroll", reflow);
  document.addEventListener("pointerdown", outside, true);
  document.addEventListener("keydown", keys, true);
  initialFocus?.focus();
  return state;
}

/**
 * Ask before doing something to the lab.
 *
 *   if (!await confirmAction(btn, { action: "STOP", target: "docker (101)" })) return;
 *
 * anchor  — the element that asked; the popover sits beside it, never on it.
 * action  — the verb, as the button says it ("STOP", "DELETE").
 * target  — what it acts on, in words the owner recognises ("docker (101)").
 * detail  — optional consequence line ("Hard power-off. Unsaved state is lost.")
 * tone    — "danger" (red, the default) or "caution" (amber) for gentle actions.
 * avoid   — extra DOMRects the popover must not cover (e.g. the menu just used).
 *
 * Resolves true on CONFIRM, false on CANCEL / Escape / click-away. Calling it
 * again for the same anchor while it is open resolves false at once and leaves
 * the open one alone: the second tap of a double tap does nothing, and only
 * the first caller ever hears "yes".
 */
export function confirmAction(anchor, { action, target, detail = "", tone = "danger", avoid = [] }) {
  if (current?.anchor === anchor && current.pop.classList.contains("guard-confirm")) return Promise.resolve(false);

  const id = `guard-${++seq}`;
  const cancel = el("button", { class: "btn btn-ghost", type: "button" }, "CANCEL");
  const confirm = el("button", {
    class: `btn guard-go arming ${tone === "danger" ? "btn-danger" : ""}`, type: "button",
    "aria-disabled": "true",
  }, `CONFIRM ${action}`);

  const pop = el("div", {
    class: `guard guard-confirm ${tone}`, role: "alertdialog", "aria-modal": "false",
    "aria-labelledby": `${id}-q`, "aria-describedby": detail ? `${id}-d` : null,
  },
    el("div", { class: "guard-tag" }, tone === "danger" ? "⚠ CONFIRM ACTION" : "▸ CONFIRM ACTION"),
    el("div", { class: "guard-q", id: `${id}-q` },
      el("b", {}, action), " ", target ? el("span", { class: "guard-target" }, target) : null, "?"),
    detail ? el("div", { class: "guard-detail", id: `${id}-d` }, detail) : null,
    el("div", { class: "guard-acts" }, cancel, confirm));

  const armedAt = performance.now() + ARM_MS;
  const state = openPopover(anchor, pop, { avoid, initialFocus: cancel, ariaPopup: "dialog" });
  setTimeout(() => {
    confirm.classList.remove("arming");
    confirm.removeAttribute("aria-disabled");
  }, ARM_MS);

  cancel.addEventListener("click", () => state.close(false));
  confirm.addEventListener("click", (e) => {
    e.stopPropagation();
    if (performance.now() < armedAt) return;   // a carried-over tap, not a decision
    state.close(true);
  });
  return state.promise;
}

/**
 * An overflow ("⋯") button holding actions that should not sit one tap away.
 *
 *   overflowMenu([{ label: "STOP", tone: "danger", select: (btn, menuRect) => … }],
 *                { title: "more actions for docker (101)" })
 *
 * select(btn, menuRect) runs after the menu closes; pass menuRect to
 * confirmAction's `avoid` so the confirm does not open where the item was.
 */
export function overflowMenu(items, { label = "⋯", title = "more actions", className = "btn btn-mini btn-ghost" } = {}) {
  const btn = el("button", {
    class: `${className} guard-more`, type: "button", title, "aria-label": title,
    "aria-haspopup": "menu", "aria-expanded": "false",
  }, label);

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (btn.disabled) return;
    if (current?.anchor === btn && current.pop.classList.contains("guard-menu")) {
      current.close(false);
      return;
    }
    const buttons = items.map(it => el("button", {
      class: `guard-item ${it.tone || ""}`, type: "button", role: "menuitem", tabindex: "-1",
    }, it.label));
    const menu = el("div", { class: "guard guard-menu", role: "menu", "aria-label": title }, ...buttons);
    const move = (d) => {
      const i = buttons.indexOf(document.activeElement);
      buttons[(i + d + buttons.length) % buttons.length].focus();
    };
    const state = openPopover(btn, menu, {
      initialFocus: buttons[0], ariaPopup: "menu",
      onKey: (k) => {
        if (k.key === "ArrowDown") { k.preventDefault(); move(1); }
        else if (k.key === "ArrowUp") { k.preventDefault(); move(-1); }
        else if (k.key === "Home") { k.preventDefault(); buttons[0].focus(); }
        else if (k.key === "End") { k.preventDefault(); buttons[buttons.length - 1].focus(); }
      },
    });
    buttons.forEach((b, i) => b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const rect = menu.getBoundingClientRect();
      state.close(false, { restoreFocus: false });
      items[i].select(btn, rect);
    }));
  });
  return btn;
}
