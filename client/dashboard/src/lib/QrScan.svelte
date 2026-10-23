<script>
  // Signing in by scanning the Phone card's QR code (Overview page, on the server).
  // A photo instead of a live camera view: browsers open the camera itself only on
  // https or localhost, and phones reach the dashboard over plain http. The button is a
  // label around the file input: some phone browsers ignore input.click() from script.
  let { onToken } = $props();

  const SIZES = [1200, 800, 1800, 2600];

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

  // The photo as something a canvas can draw: an ImageBitmap, or an <img> where there is none.
  async function picture(file) {
    if (window.createImageBitmap) {
      try {
        return await createImageBitmap(file);
      } catch {}
    }
    const img = new Image();
    img.src = URL.createObjectURL(file);
    try {
      await img.decode();
    } catch {
      throw new Error("This browser cannot read the photo.");
    }
    return img;
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
      const bitmap = await picture(file);
      // Several sizes: a smaller one smooths away the screen's moiré, a larger one finds a
      // code that is only a small part of the photo.
      let code = null;
      for (const longest of SIZES) {
        code = decode(jsQR, bitmap, longest);
        if (code?.data) break;
      }
      if (bitmap.close) bitmap.close();
      else URL.revokeObjectURL(bitmap.src);
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

<label class="scan" class:busy>
  {busy ? "Reading…" : "Scan the QR code"}
  <input
    bind:this={input}
    type="file"
    accept="image/*"
    capture="environment"
    disabled={busy}
    onchange={(ev) => ev.currentTarget.files?.[0] && read(ev.currentTarget.files[0])}
  />
</label>
{#if error}<p class="error">{error}</p>{/if}

<style>
  /* Looks like the other buttons. */
  .scan {
    position: relative;
    display: inline-block;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 12px;
    cursor: pointer;
  }
  .scan.busy {
    opacity: 0.5;
    cursor: default;
  }
  /* Out of sight but not display: none, which some phones refuse to open. */
  .scan input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
  }
</style>
