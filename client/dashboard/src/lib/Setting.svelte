<script>
  import { untrack } from "svelte";
  import { equal, format, getPath, parse } from "./schema.js";
  import { value as show } from "./format.js";

  // One leaf setting. `ctx` is shared by the whole form (see Config.svelte).
  let { path, name, desc, ctx } = $props();

  const value = $derived(getPath(ctx.draft, path));
  const saved = $derived(getPath(ctx.saved, path));
  const restart = $derived(ctx.restartRequired[path] ?? true);
  const modified = $derived(!equal(value, saved));
  const nonDefault = $derived(desc.default !== undefined && !equal(value, desc.default));
  const pending = $derived(ctx.pending.has(path));
  const commandLine = $derived(ctx.commandLine.has(path));
  const isSecretRef = $derived(name.endsWith("_env") && typeof value === "string" && value);
  const error = $derived(ctx.errors[path]);
  const id = $derived(`f-${path}`);

  // Text controls keep what was typed, even while it does not parse yet. The
  // form remounts rows after saving or discarding, which resets this.
  let text = $state(untrack(() => format(desc, getPath(ctx.draft, path))));

  function typed(next) {
    text = next;
    const result = parse(desc, next);
    if (result.error) ctx.setError(path, result.error);
    else {
      ctx.setError(path, null);
      ctx.set(path, result.value);
    }
  }

  function undo() {
    text = format(desc, saved);
    ctx.setError(path, null);
    ctx.set(path, structuredClone(saved));
  }
</script>

<div class="row" class:modified class:invalid={error}>
  <label class="name" for={id}>
    <code>{name}</code>
    {#if nonDefault}<span class="dot" title="differs from the built-in default"></span>{/if}
  </label>

  <div class="control">
    {#if desc.kind === "bool"}
      <input
        {id}
        type="checkbox"
        checked={value === true}
        disabled={!ctx.editable}
        onchange={(e) => ctx.set(path, e.currentTarget.checked)}
      />
    {:else if desc.kind === "enum"}
      <select
        {id}
        disabled={!ctx.editable}
        value={value === null ? "\u0000null" : value}
        onchange={(e) => {
          const v = e.currentTarget.value;
          ctx.set(path, v === "\u0000null" ? null : v);
        }}
      >
        {#if desc.nullable}<option value={"\u0000null"}>null</option>{/if}
        {#each desc.options as option}
          <option value={option}>{option}</option>
        {/each}
      </select>
    {:else if desc.kind === "json"}
      <textarea
        {id}
        rows={Math.min(8, Math.max(2, text.split("\n").length))}
        readonly={!ctx.editable}
        spellcheck="false"
        value={text}
        oninput={(e) => typed(e.currentTarget.value)}
      ></textarea>
    {:else}
      <input
        {id}
        type="text"
        inputmode={desc.kind === "string" ? "text" : "decimal"}
        spellcheck="false"
        autocomplete="off"
        placeholder={desc.nullable ? "null" : ""}
        readonly={!ctx.editable}
        value={text}
        oninput={(e) => typed(e.currentTarget.value)}
      />
    {/if}
    {#if isSecretRef}
      <span class="pill" class:ok={ctx.secrets[value]} class:warn={!ctx.secrets[value]}>
        {ctx.secrets[value] ? "set in .env" : "not set"}
      </span>
    {/if}
    {#if modified}
      <button class="small" type="button" onclick={undo} title={`back to ${show(saved)}`}>Undo</button>
    {/if}
  </div>

  <div class="meta">
    {#if error}<span class="err">{error}</span>{/if}
    {#if desc.bounds}<span>{desc.bounds}</span>{/if}
    <span
      class="pill"
      class:live={!restart}
      title={restart ? "takes effect after a server restart" : "applies to the running server"}
    >
      {restart ? "restart" : "live"}
    </span>
    {#if pending}
      <span class="pill warn" title="saved; the running server still uses its previous value">
        waiting for restart
      </span>
    {/if}
    {#if commandLine}
      <span class="pill warn" title="the server was started with --host/--port, which wins over the file">
        set on the command line
      </span>
    {/if}
    {#if nonDefault}
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
    padding: 8px 0 8px 8px;
    margin-left: -8px;
    border-top: 1px solid var(--line);
    border-left: 3px solid transparent;
  }
  .row.modified {
    border-left-color: var(--accent);
  }
  .row.invalid {
    border-left-color: var(--err);
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
  .invalid .control input,
  .invalid .control textarea {
    border-color: var(--err);
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
  .err {
    color: var(--err);
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--muted);
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
