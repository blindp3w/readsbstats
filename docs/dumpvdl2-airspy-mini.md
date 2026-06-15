# Running dumpvdl2 on an Airspy Mini (via IQ resampling)

`dumpvdl2` (+ libacars) is the most capable VDL2 decoder: it decodes ACARS over
VDL2 **plus** the ATN/OSI classes — ATN‑B1 **CPDLC**, **ADS‑C**, X.25/CLNP, **MIAM** —
that ACARS‑only decoders like `vdlm2dec` cannot. But it has **no native Airspy
backend**, and the Airspy Mini's fixed sample rates can't feed it directly. The
common advice is "it can't be done on an Airspy Mini." It can — this is the one
working route, tested end‑to‑end.

> **Tested on:** Raspberry Pi 4 / Ubuntu 24.04, Airspy Mini, dumpvdl2 2.6.0
> (libacars 2.2.1), [pclov3r/iq_tool](https://github.com/pclov3r/iq_tool).

## The problem

dumpvdl2 needs its input sample rate to be **`105000 × oversample`** (VDL2 is
10500 symbols/s; the default 10 samples/symbol → 105000). The Airspy Mini does only
fixed native rates (6 / 3 Msps), and **neither is a multiple of 105000**
(`6e6 / 105000 = 57.14…`, `3e6 / 105000 = 28.57…`). dumpvdl2 also has no native
Airspy backend to resample internally. So the Mini can't drive dumpvdl2 out of the box.

## What does NOT work (source‑verified — save yourself the time)

- **A native libairspy backend** — doesn't exist in dumpvdl2. Backends are
  RTL‑SDR / Mirics / SDRplay v2 / SDRplay v3 / SoapySDR only. (The README's
  "support will be added later" note has stood for years and is still absent in 2.6.0.)
- **`--soapysdr driver=airspy`** — SoapyAirspy exposes only the native 6/3 Msps and
  has no resampler. dumpvdl2 asks for e.g. 2.1 Msps, the device can't deliver it, and
  you get a process that "starts" but decodes nothing.
- **`airspy_rx -a <rate>`** — accepts only native rates; no host resampling.
- **Integer decimation** — no integer divisor of 6e6 or 3e6 lands on a 105000‑multiple.
- **soapy2tcp** — integer‑decimation only (can't reach a 105000‑multiple from the
  native rates) and CPU‑heavy.

The common thread: you need **fractional (arbitrary‑rational) resampling**, which none
of the above provides for the Airspy.

## What works: external fractional resample → IQ pipe

Resample the Airspy IQ to a 105000‑multiple **outside** dumpvdl2 and pipe raw IQ to its
stdin (`--iq-file -`, supported since dumpvdl2 v2.3.0).

**1.05 Msps = `105000 × 10`** is enough when your channels fit a ~1 MHz window. The four
common European VDL2 channels (136.725 / 136.775 / 136.875 / 136.975) span only 250 kHz,
so a 1.05 MHz window centered at 136.85 MHz covers them — and halves the CPU vs 2.1 Msps.

Recommended resampler: **[pclov3r/iq_tool](https://github.com/pclov3r/iq_tool)** — native
Airspy input → liquid‑dsp polyphase resample → cu8/cs16 on stdout, in one process.
(It's experimental / AI‑assisted, but works well here. Build with `-DWITH_AIRSPY=ON`.)

```bash
iq_tool --input airspy \
  --sdr-rf-freq 136.85e6 \
  --sdr-sample-rate 3e6 \
  --airspy-gain-mode linearity --airspy-gain-value 14 \
  --output stdout \
  --output-sample-rate 1050000 \
  --output-sample-format cu8 \
| dumpvdl2 --iq-file - \
  --sample-format U8 \
  --oversample 10 \
  --centerfreq 136.85M \
  136.725M 136.775M 136.875M 136.975M
```

Add a dumpvdl2 output sink — e.g. `--output decoded:json:udp:address=127.0.0.1,port=5556`
or `--output decoded:text:file:path=-` — and `--station-id <name>` to tag frames.

## Performance (Raspberry Pi 4)

iq_tool ≈ 0.64 of one core, dumpvdl2 ≈ 0.22 → **≈ 0.86 of one core combined**, leaving
~2.8 of the Pi 4's cores idle, with no sample drops at 1.05 Msps. (For contrast,
soapy2tcp is documented to peg all four cores.)

## Gotchas

- **Omit `--airspy-serial`.** iq_tool's serial matcher zero‑extends the 64‑bit Airspy
  serial to 128 bits, so it never matches and you get
  `airspy_open() failed AIRSPY_ERROR_NOT_FOUND`. With a single Airspy, just leave the
  flag off — it grabs the sole device. (`airspy_info` opening the device fine confirms
  it's the tool, not the hardware.)
- **Free the device first.** Only one process can hold the Airspy — stop any
  SpyServer / `vdlm2dec` / SDR++ using it, or you'll get NOT_FOUND.
- **Use `--sdr-sample-rate 3e6`** (not 6e6): half the resampler load, same result for
  these channels.
- **`U8`/cu8 first.** If weak/distant aircraft drop, switch iq_tool to `cs16` and
  dumpvdl2 to `--sample-format S16_LE` (full Airspy resolution, ~2× pipe bandwidth).
- iq_tool may mislabel the Mini as "Airspy R2" and warn about "Conflicting presets
  files" — both cosmetic when you pass every flag explicitly.
- **Wider window:** if the 1.05 MHz window underperforms, use
  `--output-sample-rate 2100000` + `--oversample 20`.

## Generic fallback (no Airspy‑specific tool)

```bash
airspy_rx -t 2 -a 3000000 -f 136.85e6 - \
  | <csdr / sox fractional resample to 1.05 Msps> \
  | dumpvdl2 --iq-file - …
```

More plumbing, same principle — use only if iq_tool misbehaves.

## Why bother

dumpvdl2 + libacars is a **strict superset** of an ACARS‑only decoder: the same
ACARS‑over‑VDL2 frames **plus** ATN‑B1 CPDLC (mandated in European continental
airspace), ADS‑C, X.25/CLNP, and MIAM — none of which ACARS‑only decoders surface. It
also reports per‑frame `sig_level` / `noise_level`. Note: one Airspy = one decoder, so
dumpvdl2 **replaces** (not augments) `vdlm2dec` unless you add a second SDR.

---

*In [readsbstats](https://github.com/blindp3w/readsbstats): set
`RSBS_VDL2_DECODER=dumpvdl2` and point the pipe's `--output decoded:json:udp:…` at the
VDL2 listener — see [operations.md](operations.md#vdl2--acars-ingest).*
