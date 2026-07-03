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
};
