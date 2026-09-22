# Jev Autopilot

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Tests](https://github.com/davidzha712/ha-jev-autopilot/actions/workflows/tests.yaml/badge.svg)](https://github.com/davidzha712/ha-jev-autopilot/actions/workflows/tests.yaml)

[中文说明](README.zh-Hans.md)

A Home Assistant integration that lets [TypeSafe Jev](https://typesafe.ai) decide
what each room needs, then carries it out with ordinary service calls.

Jev is a "System One" decision model. It reads a short description of the situation
and answers yes/no, multiple-choice and scale questions with calibrated
probabilities. It does not chat and does not generate text. Jev Autopilot writes
the questions ("Should the ceiling light be on right now?", "Which brightness
fits?"), sends one batched call per room, and turns the probabilities into actions
through deterministic rules you can inspect.

**The model advises. The code acts.** Thresholds, cooldowns, manual-override
detection and the confirmation tier all live in plain Python with tests.

> Looking for Jev as sensors, conversation agent and actions you wire into your own
> automations? Use [HA-Jev](https://github.com/AboveColin/HA-Jev). Jev Autopilot is
> the opinionated "decide and act" layer on top of the same API.

## What it does

- **Rooms, not entities.** Add a room, pick its lights, switches, fans,
  thermostats and media players, and the sensors it should look at.
- **Lights:** on/off, brightness and colour temperature on your own scales.
- **Switches and fans:** on/off. **Thermostats:** pick a heating level.
  **Media players:** turned off when nobody is watching, never turned on.
- **Ask first.** Locks, a PC outlet, a rice cooker, camera switches: anything you
  put under *Ask first* is only ever proposed to your phone with **Run** and
  **Skip** buttons. Locks can only be proposed to lock, never to unlock.
- **Respects your hands.** A change made by a person (wall switch, app, another
  automation) is detected by its context and left alone for the preset's hold time.
- **Hands back cleanly.** Existing automations you list for a room are turned off
  while the autopilot runs it and turned back on when you switch it off, when Jev is
  unreachable 3 times in a row, when the daily budget is spent, or when the API key
  is rejected.
- **Private by construction.** The text sent to Jev contains device names, states
  and sensor values. Residents appear only as a count ("2 of 3 at home"). Before
  anything is sent, including device names, room names, sensor states, the
  questions themselves and your notes:
  - any entity id Home Assistant knows, disabled ones included, is replaced with
    "a device";
  - the names of people and Home Assistant users are replaced with "a resident",
    ignoring case: the whole name, any single word of it two letters or longer, and
    for a Chinese name the given name (小明 for 王小明). A name may be followed by a
    possessive or a number ("Annas Lampe", "Bob2");
  - IPv4 addresses, and any longer run of dotted numbers around one, are replaced
    with "[address]".

  A name glued to other letters ("BobPC") is not recognised. Any other personal
  detail you type into a name or a note is sent as written. A
  device without a friendly name is sent under a name Home Assistant derives from
  its entity id.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories* → add
   `https://github.com/davidzha712/ha-jev-autopilot`, type *Integration*.
2. Install **Jev Autopilot**, restart Home Assistant.

### Manual

Copy `custom_components/jev_autopilot` into your `config/custom_components/`
directory and restart.

Requires Home Assistant 2026.9 or newer.

## Setup

*Settings → Devices & services → Add integration → Jev Autopilot.*

| Field | TypeSafe direct | OpenRouter |
|---|---|---|
| API key | TypeSafe key | OpenRouter key |
| Base URL | `https://api.typesafe.ai` | `https://openrouter.ai/api` |
| Model | `jev-latest` | `typesafe/jev-1.13` |

The flow spends one tiny call to check the key and address before saving.

Then **Add room** on the integration card. For each room:

| Field | Meaning |
|---|---|
| Lights, switches, fans, thermostats, media players | Controlled directly |
| Ask first | Locks and risky devices; only proposed to your phone |
| Context sensors | Motion, occupancy, lux, temperature, doors. Changes trigger a re-check |
| Automations to replace | Turned off while the room is under autopilot |
| Room notes | Plain-language habits, e.g. "Desk work until 18:00, cosy light after" |

An entity can belong to one room only.

## Options

*Configure* on the integration card.

| Option | Default | Notes |
|---|---|---|
| Default preset | balanced | For rooms with no preset of their own; a room's preset select overrides it |
| Ask for confirmation on | — | Mobile app notify services for *Ask first* proposals |
| Confirmation threshold | 0.90 | Floor 0.80. Minimum probability before anything is proposed |
| Daily call budget | 3000 | Rooms pause and hand back when reached; resets at midnight |
| Re-check every | 10 min | Floor 2 min. Sensor changes also trigger a check (30 s debounce) |
| Brightness levels (%) | `10, 30, 50, 75, 100` | Your scale; Jev picks a point on it |
| Colour temperature levels (K) | `2700, 3500, 4000` | Warm to cool |
| Heating levels (°C) | `off, 17, 20, 22` | `off` allowed as the first value |
| House notes | — | Habits for the whole house, sent with every call |

### Presets

| Preset | Turn on at | Turn off at | Cooldown after a change | Hold after a manual change |
|---|---|---|---|---|
| conservative | p ≥ 0.85 | p ≤ 0.15 | 15 min | 60 min |
| balanced | p ≥ 0.75 | p ≤ 0.25 | 5 min | 30 min |
| aggressive | p ≥ 0.65 | p ≤ 0.35 | 2 min | 15 min |

Between the two thresholds nothing changes, so a light does not flicker when Jev is
unsure.

## Entities

Per room (one device per room):

| Entity | Purpose |
|---|---|
| `switch.<room>_autopilot` | Turn the room's autopilot on or off |
| `select.<room>_preset` | conservative / balanced / aggressive |
| `sensor.<room>_last_decision` | What was decided and why; attributes hold the actions, Jev's probabilities, latency and tokens |
| `sensor.<room>_status` | ok / disabled / degraded / paused |
| `sensor.<room>_manual_overrides_today` | How often people overrode the room today; a tuning signal |

Global (device *Jev Autopilot*): `sensor.jev_autopilot_calls_today` and
`sensor.jev_autopilot_cost_today` (USD).

## Cost

Jev charges per input token; output is free. A room call is typically around one to
two thousand input tokens. At $0.042 per million input tokens (OpenRouter, September
2026), seven rooms checked every 10 minutes cost roughly $2–3 a month. Check the
provider's current price; the cost sensor uses the token counts the API returns.

## Troubleshooting

- **Status `degraded`:** Jev failed 3 times in a row. A repair issue names the
  error. The room's automations are already back on; it recovers by itself on the
  next successful call.
- **Status `paused`:** the daily budget is spent. Raise it in the options or wait
  for midnight.
- **Nothing happens on my phone:** check the notify service under *Ask for
  confirmation on* is a `mobile_app` service. Proposals expire after 30 minutes;
  *Skip* suppresses the same proposal for 2 hours.
- **A light keeps getting switched back:** increase the preset, add room notes, or
  look at `sensor.<room>_last_decision` attributes to see what Jev answered.
- **Diagnostics:** the integration's *Download diagnostics* contains options, rooms,
  counters and the last 50 decisions, with the API key redacted.

## Known limitations

- Jev sees what the sensors report. A room without presence or motion sensors is
  judged mostly by time of day and the state of its devices.
- Setup does not call the API; a revoked key is caught on the first room check and
  starts re-authentication.
- Colour (hue) is not controlled, only brightness and colour temperature.
- A device that reports its new state long after the autopilot's command (a slow
  light transition, a cloud-polled radiator) can be counted as a manual override and
  held for the preset's override hold.
- Removing the entry also deletes its stored decision log.

## Removal

Delete the integration under *Settings → Devices & services*. Automations it had
turned off are turned back on during unload. Removing a single room does the same
for that room. If an automation cannot be turned back on when the entry is removed
(it no longer exists, or the call fails), a notification lists it so you can turn
it on yourself.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt ruff mypy
.venv/bin/pytest --cov=custom_components.jev_autopilot
.venv/bin/ruff check custom_components tests && .venv/bin/ruff format --check custom_components tests
.venv/bin/mypy --strict --ignore-missing-imports custom_components/jev_autopilot
```

The design is in [docs/superpowers/specs](docs/superpowers/specs).

## Disclaimer

Not affiliated with TypeSafe. Jev is a trademark of its owner. You are responsible
for what your home does: keep the *Ask first* tier for anything where a wrong
decision matters.

## License

MIT
