<script>
  import { equal } from "./schema.js";
  import { value as show } from "./format.js";

  // One leaf setting. Read-only for now: the controls show the value and its type.
  let { path, name, desc, value, restart, secrets } = $props();

  const isSecretRef = $derived(name.endsWith("_env") && typeof value === "string" && value);
  const changed = $derived(desc.default !== undefined && !equal(value, desc.default));
  const id = $derived(`f-${path}`);
</script>

<div class="row" class:changed>
  <label class="name" for={id}>
    <code>{name}</code>
    {#if changed}<span class="dot" title="differs from the built-in default"></span>{/if}
  </label>

  <div class="control">
    {#if desc.kind === "bool"}
      <input {id} type="checkbox" checked={value === true} disabled />
    {:else if desc.kind === "enum"}
      <select {id} disabled>
        {#if desc.nullable}<option selected={value === null}>null</option>{/if}
        {#each desc.options as option}
          <option selected={value === option}>{option}</option>
        {/each}
      </select>
    {:else if desc.kind === "json"}
      <textarea {id} rows={Math.min(6, JSON.stringify(value, null, 2).split("\n").length)} readonly
        >{JSON.stringify(value, null, 2)}</textarea
      >
    {:else}
      <input
        {id}
        type={desc.kind === "string" ? "text" : "number"}
        value={value ?? ""}
        placeholder={value === null ? "null" : ""}
        readonly
      />
    {/if}
    {#if isSecretRef}
      <span class="pill" class:ok={secrets[value]} class:warn={!secrets[value]}>
        {secrets[value] ? "set in .env" : "not set"}
      </span>
    {/if}
  </div>

  <div class="meta">
    {#if desc.bounds}<span class="bounds">{desc.bounds}</span>{/if}
    <span class="pill" class:live={!restart} title={restart ? "applies after a server restart" : "applies to the running server"}>
      {restart ? "restart" : "live"}
    </span>
    {#if changed}
      <span class="default" title="built-in default">default {show(desc.default)}</span>
    {/if}
  </div>
</div>

<style>
  .row {
    display: grid;
    grid-template-columns: minmax(150px, 220px) minmax(0, 1fr);
    grid-template-areas: "name control" ". meta";
    gap: 4px 12px;
    padding: 8px 0;
    border-top: 1px solid var(--line);
  }
  .name {
    grid-area: name;
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .control {
    grid-area: control;
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .control input[type="text"],
  .control input[type="number"],
  .control select,
  .control textarea {
    width: 100%;
    max-width: 420px;
  }
  .control textarea {
    font-family: ui-monospace, Consolas, monospace;
    font-size: 13px;
    resize: vertical;
  }
  .meta {
    grid-area: meta;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    font-size: 12px;
    color: var(--muted);
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    flex: none;
  }
  .default {
    overflow-wrap: anywhere;
  }
  @media (max-width: 560px) {
    .row {
      grid-template-columns: minmax(0, 1fr);
      grid-template-areas: "name" "control" "meta";
    }
  }
</style>
