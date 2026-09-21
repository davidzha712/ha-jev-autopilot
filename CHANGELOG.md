# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-22

### Added

- Config flow for TypeSafe or OpenRouter, with reauthentication and reconfiguration.
- Rooms as config subentries: lights, switches, fans, thermostats, media players,
  an *Ask first* tier, context sensors, automations to replace, room notes.
- Presets (conservative, balanced, aggressive) and user-defined brightness, colour
  temperature and heating levels.
- Actionable phone notifications for *Ask first* devices; locks can only be
  proposed to lock.
- Manual-override detection by context, per-entity cooldown, daily call budget.
- Automations handed back when a room is switched off, degraded, paused, removed,
  or its key is rejected.
- Entities per room: autopilot switch, preset select, last decision, status,
  manual overrides today. Global calls and cost sensors.
- Diagnostics with the API key redacted, repair issues, English and Simplified
  Chinese translations.
