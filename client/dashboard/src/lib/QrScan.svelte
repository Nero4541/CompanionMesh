<script>
  // Signing in by scanning the Phone card's QR code (Overview page, on the server).
  // A photo instead of a live camera view: browsers open the camera itself only on
  // https or localhost, and phones reach the dashboard over plain http.
  let { onToken } = $props();

  let input = $state(null);
  let error = $state(null);
  let busy = $state(false);

  function decode(jsQR, bitmap, longest) {
    const scale = Math.min(1, longest / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    return jsQR(data, width, height, { inversionAttempts: "attemptBoth" });
  }

  function tokenIn(text) {
    try {
      return new URL(text).searchParams.get("token");
    } catch {
      return null;
    }
  }

  async function read(file) {
    busy = true;
    error = null;
    try {
      // Loaded only now: most visits never scan.
      const { default: jsQR } = await import("jsqr");
      const bitmap = await createImageBitmap(file);
      // Small first (fast), larger when the code is only a small part of the photo.
      const code = decode(jsQR, bitmap, 1600) ?? decode(jsQR, bitmap, 3200);
      bitmap.close();
      if (!code?.data) throw new Error("No QR code found in the picture: take it again, closer and straight on.");
      const token = tokenIn(code.data);
      if (!token) throw new Error("That QR code is not a dashboard login (Phone card on the Overview page).");
      onToken(token);
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
      if (input) input.value = "";
    }
  }
</script>

<input
  bind:this={input}
  type="file"
  accept="image/*"
  capture="environment"
  hidden
  onchange={(ev) => ev.currentTarget.files?.[0] && read(ev.currentTarget.files[0])}
/>
<button type="button" onclick={() => input?.click()} disabled={busy}>{busy ? "Reading…" : "Scan the QR code"}</button>
{#if error}<p class="error">{error}</p>{/if}
