// Reading the server's config JSON Schema (pydantic) to build the settings form.

function deref(root, node) {
  if (!node?.$ref) return node;
  const { $ref, ...rest } = node;
  const target = root.$defs?.[$ref.split("/").pop()] ?? {};
  return { ...deref(root, target), ...rest };
}

// What kind of control a property needs:
//   section  nested settings (properties)
//   bool | enum | integer | number | string  single value
//   json     free-form mapping or list (e.g. extra_body)
export function describe(root, raw) {
  let node = deref(root, raw);
  let nullable = false;
  if (node.anyOf) {
    const options = node.anyOf.map((n) => deref(root, n));
    const real = options.filter((n) => n.type !== "null");
    nullable = real.length < options.length;
    const { anyOf, ...rest } = node;
    node = real.length === 1 ? { ...real[0], ...rest } : { ...rest, type: "json" };
  }
  let kind;
  if (node.properties) kind = "section";
  else if (node.enum) kind = "enum";
  else if (node.type === "boolean") kind = "bool";
  else if (node.type === "integer" || node.type === "number" || node.type === "string")
    kind = node.type;
  else kind = "json";
  return {
    kind,
    nullable,
    node,
    options: node.enum ?? null,
    default: node.default,
    properties: node.properties ?? null,
    bounds: bounds(node),
  };
}

function bounds(node) {
  const lo = node.minimum ?? node.exclusiveMinimum;
  const hi = node.maximum ?? node.exclusiveMaximum;
  const lt = node.exclusiveMinimum !== undefined ? ">" : "≥";
  const gt = node.exclusiveMaximum !== undefined ? "<" : "≤";
  if (lo !== undefined && hi !== undefined) return `${lo} ${lt === ">" ? "<" : "≤"} x ${gt} ${hi}`;
  if (lo !== undefined) return `${lt} ${lo}`;
  if (hi !== undefined) return `${gt} ${hi}`;
  if (node.pattern) return "HH:MM";
  return "";
}

// Top-level properties split into sections and loose settings.
export function sections(root) {
  const loose = [];
  const out = [];
  for (const [name, raw] of Object.entries(root.properties)) {
    const d = describe(root, raw);
    if (d.kind === "section") out.push({ name, desc: d });
    else loose.push(name);
  }
  return { sections: out, loose };
}

export function equal(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// Dotted paths whose values differ between two settings objects.
export function changedPaths(a, b, prefix = "") {
  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  if (isObj(a) && isObj(b)) {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    return [...keys].flatMap((k) => changedPaths(a[k], b[k], `${prefix}${k}.`));
  }
  return equal(a, b) ? [] : [prefix.replace(/\.$/, "")];
}

export function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

export function setPath(obj, path, value) {
  const parts = path.split(".");
  const last = parts.pop();
  let o = obj;
  for (const k of parts) o = o[k] ??= {};
  o[last] = value;
}

// Text typed into a control -> { value } or { error }.
export function parse(desc, text) {
  const node = desc.node;
  if (desc.kind === "json") {
    try {
      const value = JSON.parse(text);
      if (node.type === "object" && (value === null || typeof value !== "object" || Array.isArray(value)))
        return { error: "must be a JSON object, e.g. {}" };
      return { value };
    } catch {
      return { error: "not valid JSON" };
    }
  }
  if (desc.kind === "string") {
    if (text === "" && desc.nullable) return { value: null };
    if (node.pattern && !new RegExp(node.pattern).test(text)) return { error: `expected ${desc.bounds || node.pattern}` };
    return { value: text };
  }
  // integer | number
  const t = text.trim();
  if (t === "") return desc.nullable ? { value: null } : { error: "required" };
  const n = Number(t);
  if (!Number.isFinite(n)) return { error: "not a number" };
  if (desc.kind === "integer" && !Number.isInteger(n)) return { error: "must be a whole number" };
  if (node.minimum !== undefined && n < node.minimum) return { error: `must be ≥ ${node.minimum}` };
  if (node.maximum !== undefined && n > node.maximum) return { error: `must be ≤ ${node.maximum}` };
  if (node.exclusiveMinimum !== undefined && n <= node.exclusiveMinimum)
    return { error: `must be > ${node.exclusiveMinimum}` };
  if (node.exclusiveMaximum !== undefined && n >= node.exclusiveMaximum)
    return { error: `must be < ${node.exclusiveMaximum}` };
  return { value: n };
}

// A value as the text shown in its control.
export function format(desc, value) {
  if (desc.kind === "json") return JSON.stringify(value, null, 2);
  if (value === null || value === undefined) return "";
  return String(value);
}
