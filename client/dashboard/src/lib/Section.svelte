<script>
  import Section from "./Section.svelte";
  import Setting from "./Setting.svelte";
  import { describe } from "./schema.js";

  // A group of settings; nested groups (e.g. agent.hermes) render inside it.
  let { path, desc, ctx, nested = false } = $props();

  const entries = $derived(
    Object.entries(desc.properties).map(([name, raw]) => ({
      name,
      path: `${path}.${name}`,
      desc: describe(ctx.schema, raw),
    })),
  );
  const leaves = $derived(entries.filter((e) => e.desc.kind !== "section"));
  const groups = $derived(entries.filter((e) => e.desc.kind === "section"));
  // What the group is for, one paragraph per blank-line-separated block.
  const about = $derived((desc.node.description ?? "").split(/\n\s*\n/).filter((p) => p.trim()));
</script>

<section class:nested id={nested ? undefined : `s-${path}`}>
  <h3><code>{path}</code></h3>
  {#each about as p}<p class="about">{p}</p>{/each}
  {#each leaves as e (e.path)}
    <Setting path={e.path} name={e.name} desc={e.desc} {ctx} />
  {/each}
  {#each groups as g (g.path)}
    <Section path={g.path} desc={g.desc} {ctx} nested />
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
  .about {
    margin: 0 0 8px;
    max-width: 720px;
    font-size: 13px;
    line-height: 1.45;
    color: var(--muted);
  }
</style>
