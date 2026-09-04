// Plays 24 kHz mono agent audio, resampling to the AudioContext rate.
class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = 24000 / sampleRate;
    this.queue = new Float32Array(0);
    this.readPos = 0;
    this.port.onmessage = (event) => {
      if (event.data === 'clear') {
        this.queue = new Float32Array(0);
        this.readPos = 0;
        return;
      }
      const incoming = event.data;
      const remaining = this.queue.length - Math.floor(this.readPos);
      const merged = new Float32Array(Math.max(0, remaining) + incoming.length);
      if (remaining > 0) merged.set(this.queue.subarray(Math.floor(this.readPos)), 0);
      merged.set(incoming, Math.max(0, remaining));
      this.queue = merged;
      this.readPos = 0;
    };
  }

  process(inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      const idx = Math.floor(this.readPos);
      if (idx + 1 >= this.queue.length) {
        out[i] = 0;
        continue;
      }
      const frac = this.readPos - idx;
      out[i] = this.queue[idx] * (1 - frac) + this.queue[idx + 1] * frac;
      this.readPos += this.ratio;
    }
    if (this.readPos > 96000) {
      const consumed = Math.floor(this.readPos) - 1;
      this.queue = this.queue.slice(consumed);
      this.readPos -= consumed;
    }
    return true;
  }
}
registerProcessor('playback-processor', PlaybackProcessor);
