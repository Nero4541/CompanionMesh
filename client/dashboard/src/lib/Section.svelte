<script>
  import Section from "./Section.svelte";
  import Setting from "./Setting.svelte";
  import { describe } from "./schema.js";

  // A group of settings; nested groups (e.g. agent.hermes) render inside it.
  let { root, path, desc, value, restartRequired, secrets, nested = false } = $props();

  const entries = $derived(
    Object.entries(desc.properties).map(([name, raw]) => ({
      name,
      path: `${path}.${name}`,
      desc: describe(root, raw),
    })),
  );
  const leaves = $derived(entries.filter((e) => e.desc.kind !== "section"));
  const groups = $derived(entries.filter((e) => e.desc.kind === "section"));
</script>

<section class:nested id={nested ? undefined : `s-${path}`}>
  <h3><code>{path}</code></h3>
  {#each leaves as e (e.path)}
    <Setting
      path={e.path}
      name={e.name}
      desc={e.desc}
      value={value?.[e.name]}
      restart={restartRequired[e.path] ?? true}
      {secrets}
    />
  {/each}
  {#each groups as g (g.path)}
    <Section
      {root}
      path={g.path}
      desc={g.desc}
      value={value?.[g.name]}
      {restartRequired}
      {secrets}
      nested
    />
  {/each}
</section>

<style>
  section {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 16px;
  }
  section.nested {
    margin-top: 12px;
    background: var(--bg);
  }
  h3 {
    margin: 0 0 4px;
    font-size: 15px;
  }
</style>
