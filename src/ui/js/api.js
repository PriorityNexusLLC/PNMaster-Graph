// Thin client for the local Python backend.
async function call(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.errors = (data && data.errors) || [];
    throw err;
  }
  return data;
}

export const api = {
  graph: () => call('GET', '/api/graph'),
  audit: () => call('GET', '/api/audit'),
  log: () => call('GET', '/api/log'),
  chain: () => call('GET', '/api/chain'),
  balance: () => call('GET', '/api/balance'),
  report: b => call('POST', '/api/report', b),
  stamps: () => call('GET', '/api/stamps'),
  patterns: () => call('GET', '/api/patterns'),
  path: q => call('POST', '/api/path', q),
  addEdge: e => call('POST', '/api/edges', e),
  addNode: n => call('POST', '/api/nodes', n),
  addIdentity: b => call('POST', '/api/nodes/identity', b),
};
