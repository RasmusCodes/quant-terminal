// Data layer: the live API (private terminal) or static JSON files (public build, window.RTS_STATIC = true).
export const STATIC = window.RTS_STATIC === true;
const cache = new Map();

function staticPath(path) {
  const [p] = path.split("?");
  const parts = p.replace(/^\//, "").split("/").map(encodeURIComponent);
  if (parts.length === 1) return `data/${parts[0]}.json`;
  return `data/${parts.join("/")}.json`;
}

export async function get(path, { fresh = false } = {}) {
  if (!fresh && cache.has(path)) return cache.get(path);
  const url = STATIC ? staticPath(path) : `/api${path}`;
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  const data = await r.json();
  cache.set(path, data);
  return data;
}

export async function post(path, body) {
  if (STATIC) throw new Error("This is the public, read-only build. Run the terminal locally for this action.");
  const r = await fetch(`/api${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status}`);
  return data;
}

export function invalidate(prefix = "") {
  for (const k of [...cache.keys()]) if (k.startsWith(prefix)) cache.delete(k);
}

// ---------------------------------------------------------------- static search (public build)
let index = null;
const STOP = new Set("the a an of and or for in on with to is are was were be by at as it that this from which what my i".split(" "));
const tok = (s) => (s.toLowerCase().match(/[a-z0-9][a-z0-9.\-]*/g) || []).filter((w) => !STOP.has(w) && w.length > 1);

export async function search(q, limit = 20) {
  if (!STATIC) return get(`/search?q=${encodeURIComponent(q)}&limit=${limit}`, { fresh: true });
  if (!index) index = await (await fetch("data/search.json")).json();
  const terms = tok(q);
  if (!terms.length) return [];
  const N = index.docs.length;
  const scores = new Map();
  for (const t of terms) {
    const post = index.terms[t] || [];
    const idf = Math.log(1 + N / (1 + post.length));
    for (const [i, tf] of post) scores.set(i, (scores.get(i) || 0) + idf * (tf / (tf + 1.2)));
  }
  return [...scores.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit).map(([i, s]) => ({ ...index.docs[i], score: +(s * 10).toFixed(2) }));
}
