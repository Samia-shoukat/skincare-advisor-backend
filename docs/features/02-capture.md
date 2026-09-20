# Feature 2 — Capture

**Status** Client complete · two requirements unmet on this hardware
**Requirements** FR-CAM-001 … FR-CAM-004

---

## Built

- `lib/captureGate.ts` — quality decisions from four numbers. No camera, fully unit-testable
- `lib/frameSource.ts` — vision-camera frame processor, capped at 3/sec natively
- `lib/postCaptureGate.ts` — native resize to 32px, then decode and measure
- `lib/prepareImage.ts` / `lib/uploadImage.ts` — prepare and transmit
- `screens/CaptureScreen.tsx` — production screen
- `screens/CaptureSpikeScreen.tsx` — measurement harness
- `GET /v1/scans/eligibility` — capture control gating (FR-TRI-001)

---

## Requirements

| ID | State |
| --- | --- |
| FR-CAM-001 gate before capture | **Deviation** — runs after the shutter. See ADR-014 |
| FR-CAM-001 cap at 3/sec | Met — `runAtTargetFps(3)`, enforced natively |
| FR-CAM-002 preview ≥24fps | **Not verified** — frame processor delivers nothing here |
| FR-CAM-003 prepare and transmit | Met. HTTPS enforced, loopback exempt in dev only |
| FR-CAM-004 zero-save | Met — file deleted on success, rejection and failure |

---

## Measured

Physical device, front camera: