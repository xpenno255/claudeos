// Setup: connection details per system. Secrets are write-only — the
// server stores them AES-256-GCM encrypted and only ever reports
// "stored", never the value.

import { api } from "../api.js";
import { el } from "../util.js";
import { BY_ID } from "../meta.js";

const FORMS = [
  {
    id: "unifi",
    title: "UNIFI NETWORK — UDM-SE",
    note: "Local controller API on your Dream Machine SE. Create a dedicated local admin (Settings → Admins) rather than using your Ubiquiti cloud account — MFA/cloud accounts won't work here.",
    fields: [
      { key: "host", label: "CONTROLLER URL", placeholder: "https://192.168.1.1" },
      { key: "username", label: "LOCAL ADMIN USERNAME", placeholder: "claudeos" },
      { key: "password", label: "PASSWORD", secret: true },
    ],
    tls: true,
  },
  {
    id: "proxmox",
    title: "PROXMOX VE",
    note: "Use an API token: Datacenter → Permissions → API Tokens → Add (e.g. root@pam, token id \"claudeos\", untick Privilege Separation for full access, or scope it down). The secret is shown only once.",
    fields: [
      { key: "host", label: "HOST", placeholder: "192.168.1.10:8006" },
      { key: "token_id", label: "TOKEN ID", placeholder: "root@pam!claudeos",
        hint: "full user@realm!tokenname — not just the token name" },
      { key: "token_secret", label: "TOKEN SECRET", secret: true,
        hint: "the UUID shown once when the token was created" },
    ],
    tls: true,
  },
  {
    id: "docker",
    title: "DOCKER ENGINE — UBUNTU FARM",
    note: "Point at the Docker Engine API. Safest: run tecnativa/docker-socket-proxy on the host with CONTAINERS=1 POST=1 ALLOW_RESTARTS=1 and use its port. For host CPU/RAM/GPU/disk vitals, also run a Glances sidecar on the VM (see README for the one-liner) and set its URL below.",
    fields: [
      { key: "host", label: "ENGINE API URL", placeholder: "http://192.168.1.30:2375" },
      { key: "glances_url", label: "GLANCES URL — OPTIONAL, FOR HOST VITALS", placeholder: "http://192.168.1.30:61208",
        hint: "docker run -d --pid host --network host nicolargo/glances:latest-full glances -w" },
    ],
    tls: false,
  },
  {
    id: "homeassistant",
    title: "HOME ASSISTANT — HAOS",
    note: "Create a long-lived access token: your profile (bottom-left avatar) → Security → Long-lived access tokens. Use an ADMINISTRATOR user's token — internal stats and add-on states need supervisor access.",
    fields: [
      { key: "host", label: "HA URL", placeholder: "http://192.168.1.20:8123" },
      { key: "token", label: "LONG-LIVED ACCESS TOKEN", secret: true },
    ],
    tls: true,
  },
  {
    id: "synology",
    title: "SYNOLOGY NAS — DSM",
    note: "Create a dedicated DSM user (Control Panel → User & Group) WITHOUT 2-factor auth — the API login can't answer OTP prompts. Storage Manager stats (volumes, disk health) need the administrators group; CPU/RAM works for any user.",
    fields: [
      { key: "host", label: "DSM URL", placeholder: "https://192.168.1.50:5001",
        hint: "http://…:5000 or https://…:5001" },
      { key: "username", label: "DSM USERNAME", placeholder: "claudeos" },
      { key: "password", label: "PASSWORD", secret: true },
    ],
    tls: true,
  },
  {
    id: "registries",
    title: "CONTAINER REGISTRIES — UPDATE CHECKS",
    note: "Optional credentials for the container image update checker (Containers tab). Anonymous checks work, but Docker Hub rate-limits by IP — a free PAT (hub.docker.com → Account Settings → Personal access tokens, read-only) lifts that; for GHCR use a GitHub PAT with read:packages. lscr.io needs no key.",
    fields: [
      { key: "dockerhub_user", label: "DOCKER HUB USERNAME", placeholder: "xpenno255" },
      { key: "dockerhub_token", label: "DOCKER HUB ACCESS TOKEN", secret: true },
      { key: "ghcr_user", label: "GITHUB USERNAME", placeholder: "xpenno255" },
      { key: "ghcr_token", label: "GITHUB PAT — READ:PACKAGES", secret: true },
    ],
  },
  {
    id: "ai",
    title: "CLAUDE AI — ANALYSIS ENGINE",
    note: "Powers the AI log analysis and ZHA mesh insights on the Home page. Create an API key at console.anthropic.com → API Keys. Analyses run on claude-opus-4-8; logs are sent to the Anthropic API when you click an analyse button, never automatically.",
    fields: [
      { key: "api_key", label: "ANTHROPIC API KEY", secret: true,
        hint: "sk-ant-… — stored encrypted like all other secrets" },
    ],
    tls: false,
  },
];

// Notification channels — fan-out targets for alerts (system down/recover,
// and every alerting feature built on top). All plain HTTP POST server-side.
const NOTIFY_FORMS = [
  {
    id: "ntfy",
    title: "NTFY — PUSH NOTIFICATIONS",
    note: "The quickest channel: no account needed. Pick a long random topic name (it's effectively the password), subscribe to it in the ntfy app (Android/iOS/desktop), done. Leave the server blank for ntfy.sh or point at a self-hosted instance.",
    fields: [
      { key: "host", label: "SERVER — OPTIONAL", placeholder: "https://ntfy.sh", optional: true },
      { key: "topic", label: "TOPIC", secret: true,
        hint: "long + random, e.g. claudeos-alerts-x7Q9tK2m — anyone who knows it can read your alerts" },
    ],
    tls: true,
    enabledToggle: true,
  },
  {
    id: "telegram",
    title: "TELEGRAM BOT",
    note: "Create a bot with @BotFather (/newbot) to get the token. Send your bot one message, then read your chat id from api.telegram.org/bot<TOKEN>/getUpdates (or ask @userinfobot).",
    fields: [
      { key: "bot_token", label: "BOT TOKEN", secret: true, hint: "123456789:AA… from @BotFather" },
      { key: "chat_id", label: "CHAT ID", placeholder: "123456789" },
    ],
    enabledToggle: true,
  },
  {
    id: "pushover",
    title: "PUSHOVER",
    note: "pushover.net — your user key is on the dashboard; create an Application/API token for ClaudeOS. One-time purchase per device platform after the 30-day trial.",
    fields: [
      { key: "token", label: "API TOKEN", secret: true },
      { key: "user_key", label: "USER KEY", secret: true },
    ],
    enabledToggle: true,
  },
  {
    id: "hanotify",
    title: "HOME ASSISTANT NOTIFY",
    note: "Route alerts through any HA notify service — e.g. the companion app on your phone. Uses the Home Assistant connection configured above. Find service names under Developer tools → Actions (notify.*).",
    fields: [
      { key: "service", label: "NOTIFY SERVICE", placeholder: "notify.mobile_app_pixel",
        hint: "with or without the notify. prefix" },
    ],
    enabledToggle: true,
  },
  {
    id: "webhook",
    title: "GENERIC WEBHOOK",
    note: "ClaudeOS POSTs JSON {source, title, message, priority, tags, ts} to this URL for every alert — point it at n8n, Node-RED, or anything that speaks webhooks.",
    fields: [
      { key: "host", label: "WEBHOOK URL", placeholder: "https://n8n.lan/webhook/claudeos" },
    ],
    tls: true,
    enabledToggle: true,
  },
];

export async function renderSetup(root, _args, { toast }) {
  const [config, overview] = await Promise.all([api.systems(), api.overview().catch(() => ({}))]);

  const intro = el("div", { class: "panel accent", style: "margin-bottom:16px" },
    el("div", { class: "panel-title" }, "LINK YOUR HOMELAB"),
    el("div", { class: "mono-dim", style: "font-size:12px" },
      "Credentials are encrypted at rest (AES-256-GCM, machine-local master key in data/master.key) and are never sent back to the browser. ",
      "Leave a secret field blank when editing to keep the stored value. ",
      "Most homelab gear uses self-signed certs — leave TLS verification off unless you've installed proper certificates."));
  root.append(intro);

  const grid = el("div", { class: "setup-grid" });
  for (const form of FORMS) grid.append(card(form, config[form.id], overview.systems?.[form.id], toast));
  root.append(grid);

  root.append(el("div", { class: "panel accent", style: "margin:16px 0" },
    el("div", { class: "panel-title" }, "NOTIFICATION CHANNELS"),
    el("div", { class: "mono-dim", style: "font-size:12px" },
      "Alerts — a linked system going down or recovering, plus everything the upcoming monitors add — ",
      "fan out to every enabled channel below. SAVE + TEST sends a real test notification. ",
      "Repeated identical alerts are muted for 5 minutes so a flapping box can't flood your phone.")));

  const ngrid = el("div", { class: "setup-grid" });
  for (const form of NOTIFY_FORMS) ngrid.append(card(form, config[form.id], undefined, toast));
  root.append(ngrid);
}

function card(form, cfg, status, toast) {
  const meta = BY_ID[form.id];
  const configured = cfg?.configured;
  const settings = cfg?.settings || {};

  const result = el("div", { class: "setup-result" });
  const inputs = {};

  const fieldsEls = form.fields.map(f => {
    const stored = f.secret && settings[f.key];
    const input = el("input", {
      type: f.secret ? "password" : "text",
      placeholder: stored ? "•••••  (stored — leave blank to keep)" : (f.placeholder || ""),
      value: !f.secret && settings[f.key] ? settings[f.key] : "",
      autocomplete: "off",
    });
    inputs[f.key] = input;
    return el("div", { class: "field" },
      el("label", {}, f.label, " ", stored ? el("span", { class: "secret-set" }, "● ENCRYPTED & STORED") : ""),
      input,
      f.hint ? el("div", { class: "hint" }, f.hint) : null);
  });

  let tlsCheck = null;
  if (form.tls) {
    tlsCheck = el("input", { type: "checkbox" });
    tlsCheck.checked = settings.verify_tls === true;
  }

  let enabledCheck = null;
  if (form.enabledToggle) {
    enabledCheck = el("input", { type: "checkbox" });
    enabledCheck.checked = settings.enabled !== false;
  }

  const statusPill = () => {
    if (!configured) return el("span", { class: "pill neutral" }, "NOT LINKED");
    if (form.enabledToggle) {
      return settings.enabled !== false
        ? el("span", { class: "pill ok" }, "● ENABLED")
        : el("span", { class: "pill neutral" }, "PAUSED");
    }
    if (status?.ok === true) return el("span", { class: "pill ok" }, "● ONLINE");
    if (status?.ok === false) return el("span", { class: "pill err" }, "✕ UNREACHABLE");
    return el("span", { class: "pill neutral" }, "LINKED");
  };

  async function save(thenTest = false) {
    const payload = {};
    for (const f of form.fields) {
      const v = inputs[f.key].value.trim();
      if (v || !f.secret) payload[f.key] = v;
    }
    if (form.tls) payload.verify_tls = tlsCheck.checked;
    if (form.enabledToggle) payload.enabled = enabledCheck.checked;
    const needsHost = form.fields.some(f => f.key === "host" && !f.optional);
    if (needsHost && !payload.host) {
      result.className = "setup-result err";
      result.textContent = "✕ host is required";
      return;
    }
    try {
      await api.saveSystem(form.id, payload);
      result.className = "setup-result ok";
      result.textContent = "✓ saved (secrets encrypted)";
      toast(`${meta.label} settings saved`, "ok", "SETUP");
      if (thenTest) await test();
      else setTimeout(() => location.reload(), 900);
    } catch (e) {
      result.className = "setup-result err";
      result.textContent = `✕ ${e.message}`;
    }
  }

  async function test() {
    result.className = "setup-result";
    result.textContent = "… testing link";
    try {
      const res = await api.testSystem(form.id);
      result.className = "setup-result ok";
      result.textContent = `✓ ${res.detail || "connected"}`;
      toast(res.detail || "connected", "ok", `${meta.label} TEST`);
      setTimeout(() => location.reload(), 1600);
    } catch (e) {
      result.className = "setup-result err";
      result.textContent = `✕ ${e.message}`;
      toast(e.message, "err", `${meta.label} TEST`);
    }
  }

  const removeBtn = el("button", { class: "btn btn-mini btn-danger" }, "UNLINK");
  let armed = false;
  removeBtn.addEventListener("click", async () => {
    if (!armed) { armed = true; removeBtn.textContent = "CONFIRM UNLINK?"; setTimeout(() => { armed = false; removeBtn.textContent = "UNLINK"; }, 3000); return; }
    await api.deleteSystem(form.id);
    toast(`${meta.label} unlinked`, "ok", "SETUP");
    location.reload();
  });

  return el("div", { class: "panel setup-card" },
    el("div", { class: "setup-head" },
      el("span", { class: "sys-tag", style: `background:${meta.hex};box-shadow:0 0 8px ${meta.hex}` }),
      el("h3", {}, form.title),
      statusPill()),
    status?.ok === false && status.error
      ? el("div", { class: "setup-result err", style: "margin:0 0 8px" }, `last poll: ${status.error}`)
      : null,
    el("div", { class: "setup-note" }, form.note),
    ...fieldsEls,
    form.tls
      ? el("label", { class: "check" }, tlsCheck, "verify TLS certificate (off for self-signed)")
      : null,
    form.enabledToggle
      ? el("label", { class: "check" }, enabledCheck, "channel enabled (uncheck to pause without losing settings)")
      : null,
    el("div", { class: "setup-actions" },
      el("button", { class: "btn", onclick: () => save(true) }, "SAVE + TEST"),
      el("button", { class: "btn btn-ghost", onclick: () => save(false) }, "SAVE"),
      configured ? removeBtn : null),
    result);
}
