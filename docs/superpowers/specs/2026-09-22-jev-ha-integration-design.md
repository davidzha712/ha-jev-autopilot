# Jev Autopilot — design

Date: 2026-09-22. Status: implemented in 0.1.0; where this document and the code disagree, the code and README win.

## 1. Purpose and positioning

Jev Autopilot is a Home Assistant custom integration (HACS) that uses TypeSafe's Jev
"System One" model as the decision layer for a whole house. Every few minutes, and
whenever a room's context changes, it asks Jev a batch of atomic questions about each
room ("should this light be on?", "how bright?", "what heating level?") and turns the
answers into service calls.

It is a companion to [HA-Jev](https://github.com/AboveColin/HA-Jev), not a replacement.
HA-Jev exposes Jev answers as sensors for automations the user writes. Jev Autopilot is
the opinionated "decision to action" layer: presets, hysteresis, manual-override yield,
and a human-confirmation tier for sensitive devices. Both pin `jevclient==1.2.0`, so
they install side by side.

Principle: **the model advises, deterministic code actuates.** Every threshold,
cooldown and safety rule lives in plain Python that is unit tested.

## 2. Architecture

```
config entry (API key, base_url, model)          options: preset, notify targets,
   └── subentry "room" x N                        confirm threshold, budget, levels
          │
   RoomController (one per room)
     triggers: debounced context changes + patrol timer
     ├── state.py     builds the plain-text state for Jev (no ids leaked)
     ├── engine.py    pure Python: questions + answers -> actions (no HA imports)
     ├── executes direct-tier actions with its own Context
     └── runtime.py   shared budget, stored takeovers, actionable mobile_app
                      notifications for confirm-tier actions
```

### 2.1 Configuration (UI only)

- **Setup step:** API key, base URL (default `https://api.typesafe.ai`; OpenRouter
  works with `https://openrouter.ai/api` and model `typesafe/jev-1.13`), model
  (default `jev-latest`). A one-question call validates the key. Single instance.
- **Reauth** on HTTP 401, **reconfigure** for key/base URL/model.
- **Options:** default preset, notify services for confirmations, confirm threshold
  (default 0.90, floor 0.80), daily call budget (default 3000), patrol interval
  (default 10 min, floor 2), brightness levels (default `10,30,50,75,100`),
  colour-temperature levels (default `2700,3500,4000`), heating levels
  (default `off,17,20,22`), house notes (free text sent with every state).
- **Room subentry:** name, optional area, lights, switches, fans, climates, media
  players, confirm-tier entities, context sensors, automations to yield, room notes.
  Reconfigurable.

### 2.2 Presets

| Preset | on ≥ | off ≤ | cooldown | manual override hold |
|---|---|---|---|---|
| conservative | 0.85 | 0.15 | 15 min | 60 min |
| balanced (default) | 0.75 | 0.25 | 5 min | 30 min |
| aggressive | 0.65 | 0.35 | 2 min | 15 min |

Each room has a preset select entity; it starts at the options default.

### 2.3 Questions per cycle (one batched call per room)

| Entity | Question | Action rule |
|---|---|---|
| light | Noul "on?" | hysteresis on/off thresholds |
| light with brightness | Score over brightness levels | interpolate continuous score; apply only if light ends up on and |Δ| ≥ 15 % |
| light with colour temp | Choice over Kelvin levels | confidence ≥ 0.6 and differs from current by ≥ 300 K |
| switch, fan | Noul "on?" | hysteresis |
| climate | Choice over heating levels | confidence ≥ 0.7, at most one change per 30 min |
| media player | Noul "should be turned off?" | off only, never on |
| confirm-tier | Noul, direction per domain | p ≥ confirm threshold, then ask the user |

Questions carry self-contained instructions (Jev never sees ids). The state text
lists time, weekday, sun, weather, who is home, room context sensor values, the
current state of every controlled entity, recent manual overrides, and notes.

### 2.4 Safety rules (engine)

- Cooldown per entity after any change made by the integration.
- Manual override: a controlled entity changed under a foreign context is left alone
  for the preset's hold time.
- Locks: only `lock` is ever proposed; unlock is never proposed. Locks always go
  through confirmation. Nothing in the confirm tier executes without a tap.
- Media players are only ever turned off.
- Unavailable/unknown entities are skipped.

### 2.5 Confirm tier

Notification per action with two actions, `JEVAP_RUN_<token>` and
`JEVAP_SKIP_<token>`. The integration listens to `mobile_app_notification_action`.
Tokens expire after 30 minutes. Skip suppresses the same proposal for 2 hours. A
pending proposal is never re-sent.

### 2.6 Takeover of existing automations

When a room's autopilot switch turns on, its "automations to yield" are turned off;
when it turns off, the room is removed, or the room degrades, they are turned back on.
Only automations the user listed are touched.

## 3. Failure handling and testing

### 3.1 Failures

- Timeout 20 s. One retry for 429 (honouring `retry_after`, capped at 30 s), 529 and
  connection errors.
- 401 starts reauth; every room hands its automations back until the key is fixed.
- A reply that cannot be acted on (a choice outside the offered options is dropped;
  anything else raised while deciding or acting) counts as a failure. The failure
  count resets only after a run completes.
- Home Assistant shutdown hands every room's automations back; they are taken again
  after the next start.
- 422 is a bug in question construction: logged with the offending question keys,
  counted as a failure.
- Three consecutive failed cycles mark the room **degraded**: devices hold their
  current state, yielded automations are re-enabled, a repair issue is raised and
  the notify targets get one message. The first successful cycle clears it and
  takes over again.
- Daily budget reached: cycles stop until midnight, a repair issue explains it.
- Every decision is written to a ring buffer (500 entries, `helpers.storage.Store`)
  exposed in diagnostics with the API key redacted.

### 3.2 Entities

Per room: autopilot switch, preset select, last-decision sensor (attributes: actions,
reasons, probabilities), overrides-today sensor. Global: calls today, cost today.

### 3.3 Testing

- `engine.py` unit tests: thresholds, hysteresis band, cooldown, override hold,
  brightness interpolation and deadband, colour temperature, climate rate limit,
  lock direction, media off-only, confirm threshold.
- Config flow: setup success, auth error, connection error, single instance, reauth,
  reconfigure, options validation, room subentry create and reconfigure.
- Controller with a mocked client: direct action executes, override yields, failure
  streak degrades and restores automations, confirmation executes on tap and
  suppresses on skip, budget stop.
- Diagnostics redaction. CI: ruff, pytest, hassfest, HACS action.
