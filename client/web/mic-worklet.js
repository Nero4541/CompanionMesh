// Captures mono audio, resamples to 16 kHz and posts PCM s16le chunks.
const TARGET_RATE = 16000;
const CHUNK = 1024; // samples at 16 kHz (64 ms)
const LEVEL_EVERY = 8; // report the input level every N render quanta

class MicCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.pos = 0;   // read position; -1 <= pos < 0 refers to `prev`
    this.prev = 0;  // last sample of the previous block
    this.out = new Int16Array(CHUNK);
    this.n = 0;
    this.blocks = 0;
    this.sum = 0;
    this.count = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    // Linear interpolation between sample i and i+1 (i = -1 means `prev`).
    while (this.pos < ch.length - 1) {
      const i = Math.floor(this.pos);
      const frac = this.pos - i;
      const a = i < 0 ? this.prev : ch[i];
      const b = ch[i + 1];
      const s = Math.max(-1, Math.min(1, a + (b - a) * frac));
      this.sum += s * s;
      this.count++;
      this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === CHUNK) {
        this.port.postMessage(this.out.buffer, [this.out.buffer]);
        this.out = new Int16Array(CHUNK);
        this.n = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= ch.length;
    this.prev = ch[ch.length - 1];
    if (++this.blocks % LEVEL_EVERY === 0) {
      this.port.postMessage({ level: Math.sqrt(this.sum / Math.max(1, this.count)) });
      this.sum = 0;
      this.count = 0;
    }
    return true;
  }
}

registerProcessor("mic-capture", MicCapture);
