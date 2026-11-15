# insteon-mqtt patches: raw inbound message topics

Adds two MQTT topics to [insteon-mqtt](https://github.com/TD22057/insteon-mqtt),
both disabled unless configured:

| Topic | Direction | Purpose |
|---|---|---|
| `insteon/raw/rx` | out | every inbound Insteon message, published *before* the duplicate check, with a `dup` flag |
| `insteon/raw/inject` | in | complete `02 50`/`02 51` modem frames, fed to `Protocol.inject()` |

Why: a PLM hears only what reaches its single antenna, and passes up only the
group broadcasts it holds an ALDB link for. Extra RF receivers can hear what
it missed; these topics are the seam they hand it through, which makes the
modem effectively more sensitive without adding a second transmitter. See
`../../Doc/MESH-PLAN.md`.

## Licence

**These patches are derivative works of insteon-mqtt and are offered under the
same licence, GNU GPL v3.** That includes `0002-mqtt-raw-topics.patch`, which
adds a new file (`insteon_mqtt/mqtt/Raw.py`) that becomes part of the
insteon-mqtt work. Nothing here is loaded by the `insteonrf` Python package;
it is applied to an insteon-mqtt install at image build time.

## The series

Generated, never hand-edited, from the upstream PR branch
`rf-listeners` (local checkout `/root/src/insteon-mqtt`, based on
upstream `dev` = 1.4.0). The commands are in the Containerfile header.

| Patch | Applies to |
|---|---|
| `0001-rf-listeners-code-and-schema.patch` | `Protocol.py` (`signal_raw_read`, `inject()`), new `mqtt/Raw.py`, `mqtt/Mqtt.py`, `data/config-schema.yaml` |
| `0002-rf-listeners-config-example.patch` | `config-example.yaml` (source checkout only) |

0001 is applied to **both** trees; see the Containerfile for why that
matters. The PR's tests are not in the series (the installed package has no
`tests/`); they run in the branch checkout with upstream's `pytest`.

Until 2026-11-15 this was a five-patch series against 1.3.2. The PR version
differs in one way that matters: `mqtt.Raw` is created only when the config
has a `raw:` section, so a config without it behaves exactly like stock.

## Three traps worth reading the Containerfile for

1. **The package is pip-installed into site-packages**, and
   `/opt/insteon-mqtt` is only the source checkout the hassio web CLI and docs
   live in. Patching `/opt` alone yields an image that looks patched and
   behaves exactly like stock. The build verifies by importing `insteon_mqtt`
   with no `sys.path` manipulation.
2. **The config is validated against a cerberus schema** that rejects unknown
   keys under `mqtt:` (they are parsed as user-defined discovery classes).
   Without patch 0004 the sidecar crash-loops on a Validation Error at
   startup, and the Insteon network is down until the config is reverted. The
   build now fails instead, by handing the real validator a config carrying
   the `raw:` section.

3. **1.4.0's entrypoint needs s6-overlay** (`#!/usr/bin/with-contenv sh`).
   The pod overrides the command with `/bin/sh -c` and never starts s6's
   `/init`, so the sidecar crash-looped on `s6-envdir: fatal: unable to
   envdir /run/s6/container_environment` until the build rewrote the shebang
   to `#!/bin/sh` (2026-11-15).

Also: **always use a new image tag**. The manifest's `imagePullPolicy:
IfNotPresent` means a rebuilt image pushed under an existing tag is never
pulled; the node keeps running the old one.

The base image is pinned **by digest**, not by tag: upstream publishes
`:latest`, and an unpinned pull would silently replace the patched image with a
stock one — turning injection off with no error anywhere.

## Verifying

`pytest tests/test_inject.py` applies this series to a pristine upstream tree
and exercises the result, so a version bump fails in CI rather than on a
running house:

```bash
kubectl exec homeassistant -c insteon -- tar cf - -C /opt/insteon-mqtt \
    insteon_mqtt config-example.yaml | tar xf - -C /tmp/imqtt
INSTEONRF_IMQTT=/tmp/imqtt pytest tests/test_inject.py
```

## Upstreaming

In progress: branch `rf-listeners` in `/root/src/insteon-mqtt`,
targeting upstream `dev` (their CONTRIBUTING forbids PRs against `master`).
Once merged and released, drop this directory and run the upstream image.
