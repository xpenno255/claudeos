// Shared system metadata: one identity color per system (validated dark
// categorical slots), used consistently across tiles, tabs and charts.

export const SYSTEMS = [
  { id: "unifi",         label: "UNIFI NETWORK",  tab: "network",    color: "var(--s-unifi)",   hex: "#3987e5" },
  { id: "proxmox",       label: "PROXMOX VE",     tab: "compute",    color: "var(--s-proxmox)", hex: "#199e70" },
  { id: "docker",        label: "DOCKER FLEET",   tab: "containers", color: "var(--s-docker)",  hex: "#c98500" },
  { id: "homeassistant", label: "HOME ASSISTANT", tab: "home",       color: "var(--s-ha)",      hex: "#9085e9" },
];

export const BY_ID = {
  ...Object.fromEntries(SYSTEMS.map(s => [s.id, s])),
  // not a polled system — configured on Setup, used by analysis features
  ai: { id: "ai", label: "CLAUDE AI", tab: null, color: "var(--amber)", hex: "#ffb347" },
  // notification channels — configured on Setup, used by the alert dispatcher
  ntfy:     { id: "ntfy",     label: "NTFY",      tab: null, color: "#30b48a", hex: "#30b48a" },
  webhook:  { id: "webhook",  label: "WEBHOOK",   tab: null, color: "#8a97a8", hex: "#8a97a8" },
  telegram: { id: "telegram", label: "TELEGRAM",  tab: null, color: "#2ea6da", hex: "#2ea6da" },
  pushover: { id: "pushover", label: "PUSHOVER",  tab: null, color: "#4f9cf0", hex: "#4f9cf0" },
  hanotify: { id: "hanotify", label: "HA NOTIFY", tab: null, color: "#c789d6", hex: "#c789d6" },
};
