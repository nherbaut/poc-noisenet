// The Things Stack uplink payload formatter for the NoiseNet Pico 2 W example.
// Console: Application -> Payload formatters -> Uplink -> Custom JavaScript formatter.
// Formats: see wioe5.py. Keep the ports equal to LORA_PORT / LORA_BOOT_PORT.
var MEASUREMENT_PORT = 8;
var BOOT_PORT = 9;
var RESET_NAMES = ["unknown", "power-on", "watchdog or machine.reset()", "other"];

function decodeSlots(b) {
  if (b.length < 5 || (b[0] >> 4) !== 1) {
    return { errors: ["unknown payload format"] };
  }
  var n = b[0] & 0x0F;
  if (b.length !== 2 * n + 5) {
    return { errors: ["length mismatch"] };
  }
  var slotS = b[1] * 10;
  var outOfRange = b[3 + 2 * n];
  var gaps = b[4 + 2 * n];
  var slots = [];
  for (var i = 0; i < n; i++) {
    var raw = (b[3 + 2 * i] << 8) | b[4 + 2 * i];
    slots.push({
      end_offset_s: -(n - 1 - i) * slotS,
      max_db_a_estimate: raw === 0xFFFF ? null : raw / 10,
      out_of_range: ((outOfRange >> i) & 1) === 1,
      sample_gaps: ((gaps >> i) & 1) === 1
    });
  }
  return { data: { message: "slots", sequence: b[2], slot_s: slotS, slots: slots } };
}

function decodeBoot(b) {
  if (b.length !== 10 || b[0] !== 1) {
    return { errors: ["unknown boot message format"] };
  }
  return { data: {
    message: "boot",
    firmware_version: b[1] + "." + b[2],
    reset_cause: b[3] < RESET_NAMES.length ? RESET_NAMES[b[3]] : "code " + b[3],
    slot_s: b[4] * 10,
    slots_per_uplink: b[5],
    sample_rate_hz: ((b[6] << 8) | b[7]) / 100,
    wifi_enabled: (b[8] & 1) !== 0,
    calibration_applied: (b[8] & 2) !== 0,
    adr: (b[8] & 4) !== 0,
    data_rate: b[9] === 0xFF ? null : b[9]
  } };
}

function decodeUplink(input) {
  if (input.fPort === BOOT_PORT) {
    return decodeBoot(input.bytes);
  }
  if (input.fPort === MEASUREMENT_PORT) {
    return decodeSlots(input.bytes);
  }
  return { errors: ["unexpected FPort " + input.fPort] };
}

if (typeof module !== "undefined") {
  module.exports = { decodeUplink: decodeUplink };
}
