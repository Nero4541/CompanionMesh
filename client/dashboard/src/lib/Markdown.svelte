<script>
  // A small, safe Markdown view for changelogs and release notes: headings,
  // bullet lists, paragraphs, **bold**, `code` and [links](https://…). Text is
  // never inserted as HTML, so notes fetched from GitHub cannot run scripts.
  let { text = "" } = $props();

  function inline(line) {
    const parts = [];
    const re = /\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
    let last = 0;
    for (const m of line.matchAll(re)) {
      if (m.index > last) parts.push({ t: "text", v: line.slice(last, m.index) });
      if (m[1] !== undefined) parts.push({ t: "b", v: m[1] });
      else if (m[2] !== undefined) parts.push({ t: "code", v: m[2] });
      else parts.push({ t: "a", v: m[3], href: m[4] });
      last = m.index + m[0].length;
    }
    if (last < line.length) parts.push({ t: "text", v: line.slice(last) });
    return parts;
  }

  const blocks = $derived.by(() => {
    const out = [];
    let para = null;
    let list = null;
    const flush = () => {
      if (para) out.push({ kind: "p", parts: inline(para.join(" ")) });
      if (list) out.push({ kind: "ul", items: list.map(inline) });
      para = list = null;
    };
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trimEnd();
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flush();
        out.push({ kind: `h${Math.min(heading[1].length + 2, 6)}`, parts: inline(heading[2]) });
      } else if (/^\s*[-*]\s+/.test(line)) {
        if (para) flush();
        (list ??= []).push(line.replace(/^\s*[-*]\s+/, ""));
      } else if (!line.trim()) {
        flush();
      } else if (list && /^\s{2,}/.test(raw)) {
        list[list.length - 1] += ` ${line.trim()}`;
      } else {
        if (list) flush();
        (para ??= []).push(line.trim());
      }
    }
    flush();
    return out;
  });
</script>

{#snippet parts(list)}
  {#each list as p}
    {#if p.t === "b"}<strong>{p.v}</strong>
    {:else if p.t === "code"}<code>{p.v}</code>
    {:else if p.t === "a"}<a href={p.href} target="_blank" rel="noopener noreferrer">{p.v}</a>
    {:else}{p.v}{/if}
  {/each}
{/snippet}

<div class="md">
  {#each blocks as block}
    {#if block.kind === "ul"}
      <ul>{#each block.items as item}<li>{@render parts(item)}</li>{/each}</ul>
    {:else if block.kind === "p"}
      <p>{@render parts(block.parts)}</p>
    {:else}
      <svelte:element this={block.kind}>{@render parts(block.parts)}</svelte:element>
    {/if}
  {/each}
</div>

<style>
  .md {
    font-size: 14px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  .md :global(h3),
  .md :global(h4),
  .md :global(h5),
  .md :global(h6) {
    margin: 14px 0 6px;
  }
  .md p {
    margin: 6px 0;
  }
  .md ul {
    margin: 6px 0;
    padding-left: 20px;
  }
  .md li {
    margin: 3px 0;
  }
</style>
