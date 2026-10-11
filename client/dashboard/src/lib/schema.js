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
