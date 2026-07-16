// Thin fetch wrapper over the ClaudeOS JSON API.

async function call(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch { /* non-JSON error body */ }
  if (!resp.ok) {
    throw new Error((data && data.error) || `${resp.status} ${resp.statusText}`);
  }
  return data;
}

export const api = {
  overview:        ()            => call("GET",    "/api/overview"),
  history:         ()            => call("GET",    "/api/history"),
  log:             ()            => call("GET",    "/api/log"),
  systems:         ()            => call("GET",    "/api/systems"),
  saveSystem:      (id, s)       => call("POST",   `/api/systems/${id}`, s),
  deleteSystem:    (id)          => call("DELETE", `/api/systems/${id}`),
  testSystem:      (id)          => call("POST",   `/api/systems/${id}/test`),
  pollNow:         ()            => call("POST",   "/api/poll"),

  unifiDevices:    ()            => call("GET",    "/api/unifi/devices"),
  unifiClients:    ()            => call("GET",    "/api/unifi/clients"),
  unifiInsights:   ()            => call("GET",    "/api/unifi/insights"),
  unifiEvents:     (q)           => call("POST",   "/api/unifi/events", q || {}),
  unifiAnomalies:  ()            => call("GET",    "/api/unifi/anomalies"),
  unifiTriage:     (event)       => call("POST",   "/api/unifi/events/analyze", { event }),
  unifiRestart:    (mac)         => call("POST",   `/api/unifi/devices/${mac}/restart`),
  unifiUpgrade:    (mac)         => call("POST",   `/api/unifi/devices/${mac}/upgrade`),

  proxmoxGuests:   ()            => call("GET",    "/api/proxmox/guests"),
  proxmoxNodes:    ()            => call("GET",    "/api/proxmox/nodes"),
  proxmoxStorage:  ()            => call("GET",    "/api/proxmox/storage"),
  proxmoxPerf:     ()            => call("GET",    "/api/proxmox/perf"),
  proxmoxAction:   (node, type, vmid, action) =>
    call("POST", `/api/proxmox/guests/${node}/${type}/${vmid}/${action}`),

  dockerContainers:()            => call("GET",    "/api/docker/containers"),
  dockerAction:    (id, action)  => call("POST",   `/api/docker/containers/${id}/${action}`),
  dockerStorage:   ()            => call("GET",    "/api/docker/storage"),
  scanRoots:       ()            => call("GET",    "/api/storage/roots"),
  scanFolder:      (path)        => call("POST",   "/api/storage/scan", { path }),

  reports:         ()            => call("GET",    "/api/reports"),
  reportRun:       ()            => call("POST",   "/api/reports/run"),
  reportConfig:    (c)           => call("POST",   "/api/reports/config", c),

  monitors:        ()            => call("GET",    "/api/monitors"),
  monitorsHistory: ()            => call("GET",    "/api/monitors/history"),
  monitorCreate:   (m)           => call("POST",   "/api/monitors", m),
  monitorUpdate:   (id, m)       => call("POST",   `/api/monitors/${id}`, m),
  monitorDelete:   (id)          => call("DELETE", `/api/monitors/${id}`),
  monitorsCheck:   ()            => call("POST",   "/api/monitors/check"),

  haEntities:      ()            => call("GET",    "/api/ha/entities"),
  haService:       (payload)     => call("POST",   "/api/ha/service", payload),
  haSystem:        ()            => call("GET",    "/api/ha/system"),
  haZha:           ()            => call("GET",    "/api/ha/zha"),
  haAnalyzeLogs:   ()            => call("POST",   "/api/ha/analyze-logs"),
  haZhaInsights:   ()            => call("POST",   "/api/ha/zha-insights"),
};
