// Captures microphone/meeting audio and resamples to 16 kHz mono Float32,
// emitting 20 ms chunks (320 samples) to the main thread.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const targetRate = (options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.ratio = sampleRate / targetRate;
    this.carry = new Float32Array(0);
    this.readPos = 0;
    this.out = [];
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || !input[0].length) return true;
    const block = input[0];
    const merged = new Float32Array(this.carry.length + block.length);
    merged.set(this.carry, 0);
    merged.set(block, this.carry.length);
    this.carry = merged;
    while (this.readPos + 1 < this.carry.length) {
      const idx = Math.floor(this.readPos);
      const frac = this.readPos - idx;
      this.out.push(this.carry[idx] * (1 - frac) + this.carry[idx + 1] * frac);
      this.readPos += this.ratio;
      if (this.out.length >= 320) {
        this.port.postMessage(new Float32Array(this.out.splice(0, 320)));
      }
    }
    if (this.readPos > 16000) {
      const consumed = Math.floor(this.readPos) - 1;
      this.carry = this.carry.slice(consumed);
      this.readPos -= consumed;
    }
    return true;
  }
}
registerProcessor('capture-processor', CaptureProcessor);
