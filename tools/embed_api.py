"""Drive the T-Embed over the ESPHome native API: tune, press buttons, read its log.

Runs in the esphome sidecar, which has aioesphomeapi and the API key
(``/config/secrets.yaml``). Copy it to ``/k8s/homeassistant/esphome/.tune/``
and run it there::

    kubectl exec homeassistant -c esphome -- python3 /config/.tune/embed_api.py \\
        192.168.88.156 insteon_rf_embed_tune_register=6455 --watch 120

Arguments are entity object_ids with a value (numbers, selects, switches;
any value presses a button), applied in order. ``--watch N`` then prints the
board's ``insteon_rf`` log lines for N seconds; ``--list`` lists entities.
This is how the 2026-10-08 A/Bs in Doc/T-EMBED.md were driven: the board
does not have to be adopted in Home Assistant.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

import yaml
from aioesphomeapi import (  # type: ignore[import-not-found]
    APIClient,
    ButtonInfo,
    LogLevel,
    NumberInfo,
    SelectInfo,
    SwitchInfo,
)

SECRETS = "/config/secrets.yaml"


def api_key() -> str:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: None)
    with open(SECRETS) as f:
        return str(yaml.load(f, Loader=Loader)["api_key"])


def apply(cli: Any, entity: Any, value: str) -> None:
    if isinstance(entity, NumberInfo):
        cli.number_command(entity.key, float(value))
    elif isinstance(entity, SelectInfo):
        cli.select_command(entity.key, value)
    elif isinstance(entity, SwitchInfo):
        cli.switch_command(entity.key, value.lower() in ("1", "on", "true"))
    elif isinstance(entity, ButtonInfo):
        cli.button_command(entity.key)
    else:
        raise SystemExit(f"{entity.object_id}: not a number, select, switch or button")


async def main(argv: list[str]) -> int:
    host = argv[0]
    sets = [a.split("=", 1) for a in argv[1:] if "=" in a and not a.startswith("--")]
    watch = float(argv[argv.index("--watch") + 1]) if "--watch" in argv else 0.0

    cli = APIClient(host, 6053, None, noise_psk=api_key())
    await cli.connect(login=True)
    entities, _ = await cli.list_entities_services()
    by_id = {e.object_id: e for e in entities}
    if "--list" in argv:
        for e in entities:
            print(type(e).__name__, e.object_id)
    for name, value in sets:
        if name not in by_id:
            raise SystemExit(f"no entity {name!r}; try --list")
        apply(cli, by_id[name], value)
        print(f"set {name} = {value}", flush=True)

    if watch:
        def on_log(msg: Any) -> None:
            line = re.sub(r"\x1b\[[0-9;]*m", "", msg.message.decode(errors="replace"))
            if "insteon_rf" in line:
                print(line, flush=True)

        cli.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_INFO)
        await asyncio.sleep(watch)
    await asyncio.sleep(0.5)  # let the last command go out before closing
    await cli.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
