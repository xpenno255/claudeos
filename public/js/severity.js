// One decision about what colour a status is (#86).
//
// Every view used to decide for itself, so the same severity looked different
// from one tab to the next, and two vocabularies fell through to grey: UniFi
// `VERY_HIGH` and `CRITICAL` rendered quieter than `HIGH`, and the NAS's
// `attention` volume state looked like a neutral fact. Every word the app shows
// as a status now resolves here to one of five tones, and each tone to one pill
// class and one LED class.
//
// **An unrecognised word is `unknown`, never `ok`.** A new value from an API
// update, or a typo, has to look like "nobody decided what this means" — grey —
// rather than borrow the colour of health. That is the only way this map can
// lie, so it is the one default it refuses.

// Worst first: the order `worst()` ranks by. `unknown` outranks `info` and `ok`
// because "could not tell" is not reassurance, and a summary that let a known-
// fine sibling outvote it would be.
export const TONES = ["critical", "warn", "unknown", "info", "ok"];

const SEVERITIES = {
  // The attention strip (app/attention.py): the canonical tones themselves.
  critical: "critical", warn: "warn", info: "info", ok: "ok", unknown: "unknown",
  // AI findings, report findings and lab-issue verdicts (app/ai.py, app/verdict.py).
  // `serious` keeps its own border colour on a finding card; as a status it warns.
  serious: "warn", warning: "warn",
  // UniFi event severities and triage threat levels. Everything at or above
  // HIGH is critical: VERY_HIGH and CRITICAL used to fall through to grey.
  very_high: "critical", high: "critical", medium: "warn", low: "info", none: "ok",
  // Backup job statuses (app/backups.py). `never` warns on purpose: a job added
  // and never wired up is the likeliest silent failure that tab exists to catch.
  failed: "critical", stale: "critical", anomaly: "warn", never: "warn",
  unprotected: "warn", muted: "info",
  // Proxmox SMART disk statuses (app/smart.py): ok / warn / fail.
  fail: "critical",
};

// Device and storage states that are bad whatever system reports them. Kept
// apart from the severities because a state is free text from a lab system: a
// Home Assistant sensor reading "high" is a reading, not an alarm. Which states
// are *healthy* is contextual too ("on" is fine for a light and means nothing
// for a disk), so `stateTone` takes those from the caller.
const STATES = {
  offline: "critical", exited: "critical", stopped: "critical", dead: "critical",
  unavailable: "critical", error: "critical",
  // Synology volume and disk states. `attention` is DSM asking to be looked at:
  // a warning. `degraded` is a RAID running without its redundancy.
  attention: "warn", warning: "warn", degraded: "critical", crashed: "critical",
  failing: "critical", abnormal: "critical",
};

const PILL = { critical: "err", warn: "warn", unknown: "neutral", info: "neutral", ok: "ok" };
const LED  = { critical: "err", warn: "warn", unknown: "off",     info: "off",     ok: "ok" };

function key(word) {
  return String(word ?? "").trim().toLowerCase().replace(/[\s-]+/g, "_");
}

function lookup(table, word) {
  const k = key(word);
  return Object.hasOwn(table, k) ? table[k] : "unknown";
}

/** The tone of a severity or status word, from any of the app's vocabularies. */
export function tone(word) {
  const t = lookup(SEVERITIES, word);
  return t === "unknown" ? lookup(STATES, word) : t;
}

/** The tone of a device/service state, where `okStates` names what is healthy
 *  in this context. Anything else is a known-bad state or `unknown`, never
 *  `ok`: a state nobody has named is grey. */
export function stateTone(state, okStates = []) {
  return okStates.includes(key(state)) ? "ok" : lookup(STATES, state);
}

export const pillClass = word => PILL[tone(word)];
export const ledClass = word => LED[tone(word)];

/** The worst tone among some words, or `fallback` when there are none. */
export function worst(words, fallback = "ok") {
  let best = TONES.length;
  for (const w of words) best = Math.min(best, TONES.indexOf(tone(w)));
  return best < TONES.length ? TONES[best] : fallback;
}
