#include <Adafruit_ADS1X15.h>
#include <Wire.h>

// 4 separate ADS1115 chips, one SCT per chip on differential A2-A3
#define NUM_CHIPS 4

Adafruit_ADS1115 ads[NUM_CHIPS];
const uint8_t ads_addr[NUM_CHIPS] = {0x48, 0x49, 0x4A, 0x4B};

// RMS window: 400 samples at 860 SPS = ~465ms
#define SAMPLES_PER_WINDOW 400

// Spike filter: max plausible voltage from any SCT (above = I2C glitch)
#define MAX_SAMPLE_V 2.0

// Per-channel accumulators
float sum_sq[NUM_CHIPS];
uint32_t sample_count = 0;
uint32_t seq_index = 0;  // increments once per emitted window; RAM only, resets to 0 on reboot

// Cumulative raw-signal integral per channel (centivolt-seconds), calibration-independent.
// Integrated against micros() REAL elapsed time (not an assumed sample rate) — actual
// achieved throughput has historically differed a lot from the nominal ADS1115 config
// (see commit 8a66141: 379 sps measured vs 860 sps nominal), so trusting a fixed window
// duration would silently skew every energy figure. Using the Arduino's own clock makes
// this robust to whatever the real I2C throughput turns out to be, including transient
// slowdowns. The Pi applies voltage/amp-ratio calibration to turn a delta of this counter
// into exact Wh across any gap (no guessing), as long as the Arduino itself kept running.
// uint32_t wraps after ~248 days of continuous max-scale reading; the Pi computes deltas
// with wraparound-safe (mod 2^32) arithmetic.
uint32_t cum[NUM_CHIPS];
unsigned long last_window_us;  // micros() timestamp of the previous window's cum update

void setup(void) {
  Serial.begin(115200);
  Wire.setClock(400000);

  for (int i = 0; i < NUM_CHIPS; i++) {
    if (!ads[i].begin(ads_addr[i])) {
      Serial.print("{\"event\":\"error\",\"msg\":\"ADS1115 init failed at 0x");
      Serial.print(ads_addr[i], HEX);
      Serial.println("\"}");
      while (1);
    }
    ads[i].setGain(GAIN_TWOTHIRDS);
    ads[i].setDataRate(RATE_ADS1115_860SPS);
    // Start continuous conversion on differential A2-A3
    ads[i].startADCReading(ADS1X15_REG_CONFIG_MUX_DIFF_2_3, /*continuous=*/true);
    sum_sq[i] = 0.0;
    cum[i] = 0;
  }

  Serial.println("{\"event\":\"ready\"}");
  delay(10);
  last_window_us = micros();
}

void loop(void) {
  // Wait for next conversion (~1163us at 860 SPS)
  delayMicroseconds(1163);

  // Read latest result from each chip (non-blocking)
  for (int i = 0; i < NUM_CHIPS; i++) {
    float v = ads[i].computeVolts(ads[i].getLastConversionResults());
    // Skip I2C glitches (impossible voltage for any SCT)
    if (v > MAX_SAMPLE_V || v < -MAX_SAMPLE_V) continue;
    sum_sq[i] += v * v;
    delayMicroseconds(50);  // Let I2C bus settle between chip reads
  }
  sample_count++;

  // Emit RMS after collecting enough samples
  if (sample_count >= SAMPLES_PER_WINDOW) {
    // Real elapsed time since the last cum update (unsigned subtraction is safe
    // across a single micros() rollover, ~every 71.6 min — far longer than any
    // window, including a stalled Serial.print with nobody reading the port).
    unsigned long now_us = micros();
    unsigned long dt_us = now_us - last_window_us;
    last_window_us = now_us;
    double dt_s = dt_us / 1000000.0;

    Serial.print("{\"idx\":");
    Serial.print(seq_index);
    Serial.print(",\"samples\":");
    Serial.print(sample_count);

    for (int i = 0; i < NUM_CHIPS; i++) {
      float rms = sqrt(sum_sq[i] / sample_count);
      // Add this window's true contribution (centivolt-seconds, rounded) to the
      // running cumulative integral — see 'cum' declaration for rationale.
      cum[i] += (uint32_t)(rms * 100.0 * dt_s + 0.5);
      Serial.print(",\"adc");
      Serial.print(i + 1);
      Serial.print("\":");
      Serial.print(rms, 6);
      Serial.print(",\"cum");
      Serial.print(i + 1);
      Serial.print("\":");
      Serial.print(cum[i]);
      sum_sq[i] = 0.0;
    }
    Serial.println("}");
    sample_count = 0;
    seq_index++;
  }
}
