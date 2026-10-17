<script>
  import { getToken, setToken } from "./lib/api.js";
  import Boards from "./pages/Boards.svelte";
  import Cameras from "./pages/Cameras.svelte";
  import Config from "./pages/Config.svelte";
  import Conversations from "./pages/Conversations.svelte";
  import Devices from "./pages/Devices.svelte";
  import Events from "./pages/Events.svelte";
  import Metrics from "./pages/Metrics.svelte";
  import Overview from "./pages/Overview.svelte";
  import Personas from "./pages/Personas.svelte";
  import Speech from "./pages/Speech.svelte";
  import Updates from "./pages/Updates.svelte";

  const PAGES = [
    { id: "overview", label: "Overview", component: Overview },
    { id: "metrics", label: "Latency", component: Metrics },
    { id: "config", label: "Configuration", component: Config },
    { id: "speech", label: "Speech", component: Speech },
    { id: "personas", label: "Personas", component: Personas },
    { id: "cameras", label: "Cameras", component: Cameras },
    { id: "devices", label: "Devices", component: Devices },
    { id: "boards", label: "Boards", component: Boards },
    { id: "events", label: "Events", component: Events },
    { id: "conversations", label: "Conversations", component: Conversations },
    { id: "updates", label: "Updates", component: Updates },
  ];

  function current() {
    const id = window.location.hash.replace(/^#\/?/, "");
    return PAGES.find((p) => p.id === id) ?? PAGES[0];
  }

  let page = $state(current());
  let authError = $state(null);
  let tokenInput = $state("");
  // Bumped after signing in so the page remounts and fetches again.
  let generation = $state(0);
  let hasToken = $state(Boolean(getToken()));

  $effect(() => {
    const onHash = () => (page = current());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  });

  function onAuthError(err) {
    authError = err;
  }

  function signIn(event) {
    event.preventDefault();
    setToken(tokenInput.trim());
    hasToken = true;
    tokenInput = "";
    authError = null;
    generation += 1;
  }

  function signOut() {
    setToken(null);
    hasToken = false;
    generation += 1;
  }

  const Page = $derived(page.component);
</script>

<header>
  <h1>Companion <small>admin</small></h1>
  <nav>
    {#each PAGES as p}
      <a href={`#/${p.id}`} class:active={p.id === page.id}>{p.label}</a>
    {/each}
  </nav>
  {#if hasToken}
    <button class="small" onclick={signOut}>Forget token</button>
  {/if}
</header>

<main>
  {#if authError}
    <div class="login">
      <h2>Admin access</h2>
      {#if authError.status === 403}
        <p>{authError.message}.</p>
        <p class="muted">
          Open this page on the server itself (http://127.0.0.1:…/admin/), or set an admin token in
          the server's <code>.env</code>, restart it and enter that token here.
        </p>
      {:else}
        <p>This server needs its admin token (<code>COMPANION_ADMIN_TOKEN</code> in its .env).</p>
        <p class="muted">The device token does not work here.</p>
      {/if}
      <form onsubmit={signIn}>
        <input
          type="password"
          autocomplete="off"
          placeholder="Admin token"
          bind:value={tokenInput}
          aria-label="Admin token"
        />
        <button type="submit" disabled={!tokenInput.trim()}>Sign in</button>
      </form>
    </div>
  {:else}
    {#key `${page.id}:${generation}`}
      <Page {onAuthError} />
    {/key}
  {/if}
</main>

<style>
  header {
    display: flex;
    align-items: center;
    gap: 8px 24px;
    flex-wrap: wrap;
    padding: 12px 0;
    border-bottom: 1px solid var(--line);
    margin-bottom: 16px;
  }
  h1 {
    font-size: 20px;
    margin: 0;
  }
  h1 small {
    color: var(--muted);
    font-weight: normal;
    font-size: 14px;
  }
  nav {
    display: flex;
    gap: 4px;
    flex: 1;
    flex-wrap: wrap;
  }
  nav a {
    color: var(--fg);
    text-decoration: none;
    padding: 6px 12px;
    border-radius: 8px;
  }
  nav a:hover {
    background: var(--panel);
  }
  nav a.active {
    background: var(--accent);
    color: var(--on-accent);
  }
  .login {
    max-width: 480px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px 20px;
  }
  .login h2 {
    margin-top: 0;
  }
  .login form {
    display: flex;
    gap: 8px;
    margin-top: 12px;
  }
  .login input {
    flex: 1;
    min-width: 0;
  }
</style>
