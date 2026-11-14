"""
Load a channel from its manifest.yaml and register its Channel instance.

Scans the channel module for a :class:`~channels.base.Channel` subclass,
instantiates it, and registers the instance.
"""

import importlib
from pathlib import Path
from typing import Any

import yaml

from bus.registry import register_channel
from channels.base import Channel


def load_channel(channel_dir: Path, config: dict | None = None, **deps: Any) -> None:
    """
    1. Read <channel_dir>/manifest.yaml.
    2. Validate kind == "channel".
    3. Import the handler module, find a `Channel` subclass.
    4. Instantiate → register_channel(name, instance).

    Extra keyword arguments (e.g. `scheduler=…`, `session_pool=…`)
    are forwarded to the Channel constructor.
    """
    manifest_path = channel_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Channel manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    if manifest.get("kind") != "channel":
        raise ValueError(
            f"Expected kind='channel' in {manifest_path}, "
            f"got '{manifest.get('kind')}'"
        )

    name = manifest["name"]
    module_name = f"channels.{channel_dir.name}.handler"
    module = importlib.import_module(module_name)

    # Find the first Channel subclass in the module
    channel_cls = None
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if (
            isinstance(obj, type)
            and issubclass(obj, Channel)
            and obj is not Channel
        ):
            channel_cls = obj
            break

    if channel_cls is None:
        raise AttributeError(
            f"No Channel subclass found in {module_name}.py"
        )

    instance = channel_cls(config=config, **deps)
    register_channel(name, instance)
    print(f"[bus] Channel loaded: {name} ({channel_cls.__name__})")
