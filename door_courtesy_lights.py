"""door_courtesy_lights - turn on a wall light when its door opens at night,
keep it on while someone is present at that location, and turn it off a
configurable time after both the door is closed and presence has cleared.

Replaces the old 3-automation HA system (open -> on, close -> start timer,
timer-finished -> conditional off). All off-timing is delegated to the
``device_controller`` app so the nightly sweep and this app can't fight over the
same light.

Behaviour per door:

* Door OPEN (at night): turn the light on and place an **indefinite hold** (the
  light must stay on while the door is open). Presence alone never turns a light
  on - only a door-open does (these are courtesy lights, not motion lights).
* While the door is closed but **presence** is detected at that location, the
  light is held on.
* Once the door is closed **and** presence is clear, the off is scheduled for
  ``off_delay`` later. Any new door-open or fresh presence cancels that and
  re-holds; so ``off_delay`` effectively counts from the *last* activity (door
  close or presence clear) and doubles as the presence debounce.

A manual off is honoured: the app stops managing the light (until the next
door-open) and releases its hold so it never blocks the nightly sweep.

``off_delay``, ``brightness``, ``night_margin`` and ``presence`` may be set
globally as defaults and overridden per door. A per-mapping value always wins.
A mapping may drive one light (``light:``) or several (``lights: [a, b]``).

Presence sources (per mapping, list under ``presence``) are each either:
  * a state sensor - ``binary_sensor.office_motion_sensor`` (active when "on"),
    or ``{entity: ..., active_state: "on"}``; or
  * the AP check - ``{type: ap}`` (uses the app-level ``ap_presence`` config,
    optionally overriding ``ap_name`` / ``ap_entity`` / ``ap_mac`` /
    ``person_entities`` - so different doors can watch different access points).

A door may have **no presence** (omit ``presence`` or set it to ``[]``) when
there is no good way to sense someone outside it; that light simply goes off
``off_delay`` after the door closes. An access point may be named by friendly
name (``ap_name``) or entity id (``ap_entity``); its MAC is read from that
``device_tracker``'s ``mac`` attribute at runtime.
"""
import time
import uuid

import hassapi as hass

from home_lib import ControllerClient, PresenceMixin


class DoorCourtesyLights(PresenceMixin, hass.Hass):
    def initialize(self):
        self.client = ControllerClient(
            self, self.args.get("controller", "device_controller")
        )
        # Top-level values act as defaults; each mapping may override them.
        default_off_delay = int(self.args.get("off_delay_seconds", 300))
        default_brightness = int(self.args.get("brightness_pct", 75))
        default_night_margin = str(self.args.get("night_margin", "00:30:00"))
        default_grace = int(self.args.get("off_valid_grace_seconds", 1800))
        default_presence = self.args.get("presence", []) or []
        # Test/debug aid: bypass the night gate (light comes on regardless of
        # time of day). Per-door overridable. Default off.
        default_ignore_night = bool(self.args.get("ignore_night", False))

        # App-level AP defaults (used by any presence source of type "ap").
        ap = self.args.get("ap_presence", {}) or {}
        self.default_ap = self._ap_ref(ap)
        self.default_persons = ap.get("person_entities", []) or []

        # Indexes built while parsing mappings.
        self.doors = {}                # door entity -> cfg
        self.sensor_index = {}         # presence sensor entity -> [cfg, ...]
        self.light_index = {}          # light entity -> cfg
        uses_ap = False

        for m in self.args.get("mappings", []):
            entity = m["door"]
            presence = self._parse_presence(m.get("presence", default_presence))
            lights = m.get("lights") or [m["light"]]
            cfg = {
                "door": entity,
                "lights": list(lights),
                "open_state": str(m.get("open_state", "on")),
                "off_delay": int(m.get("off_delay_seconds", default_off_delay)),
                "brightness": int(m.get("brightness_pct", default_brightness)),
                "night_margin": str(m.get("night_margin", default_night_margin)),
                "grace": int(m.get("off_valid_grace_seconds", default_grace)),
                "presence": presence,
                "ignore_night": bool(m.get("ignore_night", default_ignore_night)),
                # shared runtime state
                "door_open": False,
                # per-light runtime state: light -> {managing, session, hold_mode}
                "lstate": {lt: {"managing": False, "session": None,
                                "hold_mode": "none"} for lt in lights},
            }
            self.doors[entity] = cfg
            for lt in lights:
                self.light_index[lt] = cfg
            self.listen_state(self._on_door, entity)

            for src in presence:
                if src["kind"] == "state":
                    self.sensor_index.setdefault(src["entity"], []).append(cfg)
                elif src["kind"] == "ap":
                    uses_ap = True

            self.log("mapped %s -> %s (off_delay=%ss brightness=%s%% "
                     "night_margin=%s presence=%d src)"
                     % (entity, ", ".join(lights), cfg["off_delay"],
                        cfg["brightness"], cfg["night_margin"], len(presence)))

        # React to physical presence sensors immediately.
        for sensor in self.sensor_index:
            self.listen_state(self._on_presence, sensor)

        # React when a managed light turns off (controller off or manual off) so
        # we stop managing and release our hold.
        for light in self.light_index:
            self.listen_state(self._on_light_off, light, new="off")

        # AP presence is derived, so poll it if any door uses it.
        if uses_ap:
            interval = int(self.args.get("poll_interval_seconds", 60))
            self.run_every(self._poll_ap, "now+%d" % interval, interval)
            self.log("AP presence polling every %ds." % interval)

        # Re-assert holds for any door currently open at night (the controller
        # drops indefinite holds across a restart).
        for cfg in self.doors.values():
            if self.get_state(cfg["door"]) == cfg["open_state"] \
                    and self._night_ok(cfg):
                cfg["door_open"] = True
                for ls in cfg["lstate"].values():
                    ls["managing"] = True
                self._update(cfg)

        self.log("DoorCourtesyLights initialized for %d door(s)" % len(self.doors))

    # ------------------------------------------------------------------ #
    # Config parsing
    # ------------------------------------------------------------------ #
    def _ap_ref(self, cfg):
        """Pick a single access-point reference from a config block. Accepts
        ``ap_mac`` / ``ap_entity`` / ``ap_name`` (in that precedence); the mixin
        auto-detects which kind it is. Returns "" if none given."""
        return (cfg.get("ap_mac") or cfg.get("ap_entity")
                or cfg.get("ap_name") or "")

    def _parse_presence(self, raw):
        sources = []
        for src in raw or []:
            if isinstance(src, str):
                sources.append({"kind": "state", "entity": src,
                                "active_state": "on"})
            elif isinstance(src, dict) and src.get("type") == "ap":
                # Per-source AP override (mac/entity/name); else app default.
                sources.append({
                    "kind": "ap",
                    "ap": self._ap_ref(src) or self.default_ap,
                    "persons": src.get("person_entities", self.default_persons),
                })
            elif isinstance(src, dict) and "entity" in src:
                sources.append({"kind": "state", "entity": src["entity"],
                                "active_state": str(src.get("active_state", "on"))})
        return sources

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _is_night(self, night_margin):
        return self.now_is_between("sunset - %s" % night_margin, "sunrise")

    def _night_ok(self, cfg):
        """True if this door should manage its light right now (night, or the
        ignore_night test flag is set)."""
        return cfg["ignore_night"] or self._is_night(cfg["night_margin"])

    def _hold_id(self, light):
        return "courtesy:%s" % light

    def _epoch_in(self, seconds):
        return time.time() + float(seconds)

    def _presence_active(self, cfg):
        for src in cfg["presence"]:
            if src["kind"] == "state":
                if self.get_state(src["entity"]) == src["active_state"]:
                    return True
            elif src["kind"] == "ap":
                if self.person_on_ap(src["ap"], src["persons"]):
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Core state machine
    # ------------------------------------------------------------------ #
    def _update(self, cfg):
        """Re-evaluate whether each managed light should stay on (door open or
        presence) or start its off countdown. Acts per light we are managing."""
        stay_on = cfg["door_open"] or self._presence_active(cfg)
        for light in cfg["lights"]:
            ls = cfg["lstate"][light]
            if not ls["managing"]:
                continue
            if stay_on:
                if ls["session"] is not None:
                    self.client.cancel_off(light, ls["session"])
                    ls["session"] = None
                if ls["hold_mode"] != "indef":
                    self.client.hold(light, self._hold_id(light), until=None,
                                     source=cfg["door"])
                    ls["hold_mode"] = "indef"
            else:
                # Door closed and clear: arm the off countdown once.
                if ls["session"] is None:
                    off_delay = cfg["off_delay"]
                    session = "%s:%s:%s" % (cfg["door"], light,
                                            uuid.uuid4().hex[:8])
                    ls["session"] = session
                    self.client.schedule_off(light, session, off_delay,
                                             valid_for=off_delay + cfg["grace"],
                                             source=cfg["door"])
                    self.client.hold(light, self._hold_id(light),
                                     until=self._epoch_in(off_delay),
                                     source=cfg["door"])
                    ls["hold_mode"] = "finite"
                    self.log("%s closed & clear -> %s off in %ss (session %s)"
                             % (cfg["door"], light, off_delay, session))

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #
    def _on_door(self, entity, attribute, old, new, kwargs):
        cfg = self.doors.get(entity)
        if cfg is None:
            return
        opened = new == cfg["open_state"]
        closed = old == cfg["open_state"] and new != cfg["open_state"]

        if opened:
            if not self._night_ok(cfg):
                return
            cfg["door_open"] = True
            for light in cfg["lights"]:
                cfg["lstate"][light]["managing"] = True
                self.client.turn_on(light, brightness_pct=cfg["brightness"])
            self.log("door open %s -> on %s"
                     % (entity, ", ".join(cfg["lights"])))
            self._update(cfg)
        elif closed:
            cfg["door_open"] = False
            self._update(cfg)

    def _on_presence(self, entity, attribute, old, new, kwargs):
        for cfg in self.sensor_index.get(entity, []):
            self._update(cfg)

    def _poll_ap(self, kwargs):
        for cfg in self.doors.values():
            if any(s["kind"] == "ap" for s in cfg["presence"]):
                self._update(cfg)

    def _on_light_off(self, entity, attribute, old, new, kwargs):
        cfg = self.light_index.get(entity)
        if cfg is None:
            return
        ls = cfg["lstate"].get(entity)
        if ls is None or not ls["managing"]:
            return
        # Light went off (controller off, or a manual off). Stop managing this
        # light and release its hold so we never block the nightly sweep.
        if ls["session"] is not None:
            self.client.cancel_off(entity, ls["session"])
            ls["session"] = None
        self.client.release(entity, self._hold_id(entity))
        ls["hold_mode"] = "none"
        ls["managing"] = False
        self.log("%s went off -> stop managing" % entity)
