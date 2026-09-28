# /brag plan: Tortoise-UFH

## Inspect: the answers

- **What is it?** A Home Assistant integration that runs its own controller for every room's
  underfloor heating (and floor cooling), with an optional split/AC as a fast helper.
- **Who is it for, and what does it do for them?** Home Assistant users with underfloor
  heating in a heavy concrete slab. Each room's valve is steered by a PI + trend-damping loop
  built for a slab with a 4–6 h time constant, so rooms reach the setpoint without
  overshooting, and the split only helps when a room is far from it.
- **What sets it apart?** The controller is designed around the slab's inertia (trend
  damping, anti-windup), the floor always stays the base source (the split never takes
  over), floor cooling has two layers of dew-point protection, and the whole algorithm is
  tested on a built-in digital twin that runs the same code as Home Assistant.
- **Most impressive claim:** on the digital twin (`steady_heating`, 48 h, 0 °C outside) the
  real controller settles on the 21 °C setpoint with a peak of **+0.18 K** (the same number
  the repo's simulation gate documents). Re-run for this video: `steady_heating` peak 21.181 °C.
- **Visual hook:** the new logo's loop, a real bifilar ("ślimak") UFH loop, filling with
  hot water, then the camera pulls out to show it is the shell of a tortoise.
- **Real UI to show:** the Tortoise-UFH sidebar panel itself (`tortoise-ufh-panel.js`,
  mounted with sample data like `dev/panel-preview.html`): the Rooms table with the per-room
  Off/Live control, and the Manifolds tab's drawing with live valve openings.
- **Tone:** `default` (punchy, playful, clean), framed by the project's own fable.
- **Share caption:** "The slow tortoise carries the load. The fast hare closes the gap."

## Angle

The project's name is the pitch. Underfloor heating is slow, so give it a tortoise: a
controller that is patient on purpose and always lands on the setpoint, with the split as
the hare that only closes the gap.

## Identity

- Colours from `scripts/brand/logo.mjs`: petrol shell `#153E44`, rim `#2E6F6C`, skin
  `#7DBAAE`, supply `#FF6B3D`, return `#FFBC4E`. Background a deep petrol gradient.
- Type: Nunito ExtraBold / Bold (the wordmark's font), white `#F2F7F6` on petrol.
- The panel is shown in Home Assistant's dark theme, inside a rounded window.

## Storyboard (landscape 1920×1080, 30 fps, 21 s)

| # | Time | Scene | On screen | Motion |
|---|---|---|---|---|
| 1 | 0.0–3.0 | Hook | Close-up of the shell's loop. Headline: **"Underfloor heating is slow."** | Empty pipes fill with hot water: orange supply spirals in, turns at the S-bend, amber return winds back out; a warm glow grows. |
| 2 | 3.0–7.0 | Reveal | The loop is a tortoise's shell. Wordmark **"tortoise-ufh"**, line: **"Per-room underfloor heating control for Home Assistant."** | On the downbeat the camera pulls out, head, legs and tail pop in, the tortoise takes two slow steps to the left; wordmark and line slide in on the right. |
| 3 | 7.0–11.0 | Product in use | The real sidebar panel (Rooms tab). **"One controller per room."** then **"Every valve, live."** | Panel window rises in. A cursor switches the Bedroom from Off to Live and its valve opens; the cursor clicks Manifolds and the manifold drawing appears. |
| 4 | 11.0–15.0 | Proof | Chart of the real controller on the digital twin: room temperature 20 → 21 °C over 48 h, valve underneath. **"Slow slab. Soft landing."** Callout **"+0.18 K"** peak over setpoint. Footnote: "48 h on the built-in digital twin". | Axes draw in, the curve draws left to right, the setpoint line and the callout land at the end. |
| 5 | 15.0–18.0 | Punchline | **"The slow tortoise carries the load."** / **"The fast hare closes the gap."** with small labels *underfloor heating* / *split / AC*. | Lines rise in one after the other; "tortoise" and "hare" in orange. |
| 6 | 18.0–21.0 | End card | Tortoise mark + wordmark, **"Install via HACS"**, `github.com/hubertciebiada/tortoise-ufh`. | Lockup settles, the loop glows once. |

Durations: 3 + 4 + 4 + 4 + 3 + 3 = 21 s.

## Sound

One original track, synthesized for this video: 120 BPM in D major, warm and steady.
Intro (0–3 s) is a soft pad with a plucked arpeggio and a quiet water shimmer, rising into
the downbeat at 3.0 s where a round kick, bass and soft clap enter (D – Bm – G – A per
bar). The punchline (15 s) drops the drums to a light shaker; the end card (18 s) resolves on
D with a soft bell. UI clicks are short tuned plucks and the transitions are soft filtered
whooshes, all in the same key and the same reverb, mixed under the music.

## Poster

The settled reveal frame (tortoise + wordmark + line, t ≈ 6.4 s), also baked in as frame 0.
