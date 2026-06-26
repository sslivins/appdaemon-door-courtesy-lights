# appdaemon-door-courtesy-lights

An [AppDaemon](https://appdaemon.readthedocs.io/) app for Home Assistant that
turns on courtesy light(s) when a door opens at night, then turns them off a
configurable delay after the door closes - keeping them on while someone is
still nearby.

> Personal project, shared as-is. Adapt the entity IDs in
> `door_courtesy_lights.yaml` to your own setup.

## How it works

- Each mapping pairs a door (`binary_sensor`) with one light (`light:`) or
  several (`lights: [a, b]`).
- When the door opens during the **night window** (from `night_margin` before
  sunset until sunrise), the light(s) come on at `brightness_pct`.
- The light(s) turn off `off_delay_seconds` after the door closes **and**
  presence is clear.
- **Presence keeps a light on but never turns it on.** A presence source is
  either a state sensor (e.g. a motion sensor) or an access-point check (a
  household phone connected to a named AP). Omit `presence` (or set `[]`) for a
  door with no good way to sense someone outside - those lights just go off after
  the door closes.
- Per-mapping overrides: `off_delay_seconds`, `brightness_pct`, `night_margin`,
  `presence`, and a test-only `ignore_night` flag.

Lights are switched through the
[`appdaemon-device-controller`](https://github.com/sslivins/appdaemon-device-controller)
so this app coexists safely with other apps that manage the same lights.

## Dependencies

- [`appdaemon-home-lib`](https://github.com/sslivins/appdaemon-home-lib)
  (`ControllerClient` + `PresenceMixin`)
- [`appdaemon-device-controller`](https://github.com/sslivins/appdaemon-device-controller)
  (the arbitration app it sends holds/off-requests to)

## Deploy

```sh
cd conf/apps
git clone https://github.com/sslivins/appdaemon-door-courtesy-lights door_courtesy_lights
```

Then edit `door_courtesy_lights.yaml` with your doors, lights, and presence
sources.
