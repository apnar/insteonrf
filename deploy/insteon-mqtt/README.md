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

| Patch | Applies to |
|---|---|
| `0001-protocol-raw-signal-and-inject.patch` | `Protocol.py`: a `signal_raw_read` signal and the `inject()` method |
| `0002-mqtt-raw-topics.patch` | new `mqtt/Raw.py` |
| `0003-wire-raw-topics.patch` | `mqtt/Mqtt.py`, `mqtt/__init__.py` |
| `0004-config-schema-raw-section.patch` | `data/config-schema.yaml` |
| `0005-document-raw-in-config-example.patch` | `config-example.yaml` (source checkout only) |

0001–0004 are applied to **both** trees; see the Containerfile for why that
matters.

## Two traps worth reading the Containerfile for

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

The raw topics are generally useful and a merged feature beats a carried
patch. Worth offering to TD22057 rather than maintaining this indefinitely.
