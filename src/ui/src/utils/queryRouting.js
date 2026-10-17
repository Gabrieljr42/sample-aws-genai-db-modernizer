// #822: the rows of the results page's query routing table, one per query of
// the assessment (GET /assessments/{job_id}/query-journeys), so every query's
// journey and "Why this engine" trace opens without schema design. Rows only
// format what the API and the facts layer hold (routingKindsById in
// reportFacts.js); nothing here decides or recomputes a query's routing.

const EXCERPT_CHARS = 120;

// The start of a query's SQL, whitespace collapsed, about 120 characters (the
// same length the engineering report's trace appendix prints).
export function queryExcerpt(text) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  if (flat.length <= EXCERPT_CHARS) return flat;
  return `${flat.slice(0, EXCERPT_CHARS).trimEnd()}…`;
}

export function buildQueryRoutingRows(journeys, kindsById, engineLabels) {
  return (journeys || []).filter(j => j && j.query_id).map((j) => {
    const engine = (j.assignment && j.assignment.assigned_engine) || null;
    return {
      queryId: j.query_id,
      excerpt: queryExcerpt(j.source && j.source.query_text),
      tables: (j.source && j.source.tables_accessed) || [],
      queryType: (j.source && j.source.query_type) || null,
      engine,
      engineName: engine ? (engineLabels[engine] || engine) : null,
      kinds: kindsById.get(j.query_id) || null,
    };
  });
}

// mode 'all': every query; 'listed': only the queries whose routing has a
// story (the facts list them). The text matches the query id, its statement,
// its query type, a table or the engine name, case-insensitively. `only`
// narrows to one query type and/or one engine id (#1028); null means any.
export function filterQueryRoutingRows(rows, text, mode, only = {}) {
  const needle = String(text || '').trim().toLowerCase();
  const { queryType = null, engine = null } = only || {};
  return rows.filter((r) => {
    if (mode === 'listed' && !r.kinds) return false;
    if (queryType && r.queryType !== queryType) return false;
    if (engine && r.engine !== engine) return false;
    if (!needle) return true;
    return [r.queryId, r.excerpt, r.queryType || '', r.engineName || '', ...r.tables]
      .some(v => String(v).toLowerCase().includes(needle));
  });
}

// The query types and engines the rows hold, for the filters' options (#1028):
// each distinct value once, sorted, never a count.
export function queryRoutingFilterOptions(rows) {
  const types = new Set();
  const engines = new Map();
  (rows || []).forEach((r) => {
    if (r.queryType) types.add(r.queryType);
    if (r.engine) engines.set(r.engine, r.engineName || r.engine);
  });
  return {
    queryTypes: [...types].sort(),
    engines: [...engines.entries()]
      .map(([engine, engineName]) => ({ engine, engineName }))
      .sort((a, b) => a.engineName.localeCompare(b.engineName)),
  };
}
