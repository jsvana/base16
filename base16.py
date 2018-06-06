#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path
from typing import Any

import requests

MAGIC_STRING = "Written by base16 manager, do not modify manually"
SUPPORTED_PLUGINS = ["xresources", "dunst", "i3"]


class PathNotFoundError(Exception):
    """Raised when attempting to fetch an invalid path"""


class Config:

    DEFAULTS = {"enabled": []}

    def __init__(self, path: Path) -> None:
        self.path = path
        with self.path.open() as f:
            self.config = json.load(f)

    def __getattr__(self, attr: str) -> Any:
        if attr not in self.config:
            if attr not in self.DEFAULTS:
                raise AttributeError(
                    f"{attr} is not a configured option and has no defaults"
                )
            return self.DEFAULTS[attr]

        return self.config[attr]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--config-path",
        type=Path,
        default=Path.home() / ".config/base16/config",
        help="Path to configuration file (default is %(default)s)",
    )

    theme_arg = argparse.ArgumentParser(add_help=False)
    theme_arg.add_argument("theme", help="Theme to get")

    subparsers = parser.add_subparsers()
    subparsers.required = True

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check your base16 setup for proper configuration"
    )
    doctor_parser.set_defaults(cmd=cmd_doctor)

    install_parser = subparsers.add_parser(
        "install", parents=[theme_arg], help="Install the given Base16 theme"
    )
    install_parser.set_defaults(cmd=cmd_install)

    return parser.parse_args()


def sync_xresources(config: Config) -> bool:
    proc = subprocess.run(["xrdb", "-merge", config.path])
    if proc.returncode != 0:
        print("Error running xrdb", file=sys.stderr)
        return False
    return True


ConfigInfo = namedtuple("ConfigInfo", ["comment", "path", "theme_url", "post_process"])
CONFIG_INFO = {
    "xresources": ConfigInfo(
        "!",
        Path.home() / ".Xresources",
        "https://raw.githubusercontent.com/chriskempson/base16-xresources/master/xresources/base16-{}.Xresources",
        sync_xresources,
    ),
    "dunst": ConfigInfo(
        "#",
        Path.home() / ".config/dunst/dunstrc",
        "https://raw.githubusercontent.com/khamer/base16-dunst/master/themes/base16-{}.dunstrc",
        None,
    ),
    "i3": ConfigInfo(
        "#",
        Path.home() / ".config/i3/config",
        "https://raw.githubusercontent.com/khamer/base16-i3/master/colors/base16-{}.config",
        None,
    ),
}


def validate_config(config_name: str) -> bool:
    config = CONFIG_INFO.get(config_name)
    if config is None:
        print(f"{config_name} is not a supported configuration file", file=sys.stderr)
        return False

    base_path = config.path.parent / (config.path.name + ".base")
    if not config.path.is_file():
        if not base_path.is_file():
            print(
                "No {config_name} or {config_name}.base file exist. Please create "
                f"a base configuration at {base_path}. base16 take that and "
                "concatenate it with the downloaded theme file.",
                file=sys.stderr,
            )
            return False
        return True

    with config.path.open() as f:
        lines = f.readlines()

    if not lines:
        return True

    if len(lines) < 2:
        print(f'"{config.path}" has an invalid header', file=sys.stderr)
        return False

    if lines[0].strip() != f"{config.comment} {MAGIC_STRING}":
        print(
            f'"{config.path}" does not appear to be managed by base16. Move your '
            f'existing {config.path.name} file to "{config.path.name}.base" and re-run, and '
            "base16 will concatenate that file with the downloaded theme file",
            file=sys.stderr,
        )
        return False

    m = re.match(f"{config.comment} Generated (\d+)$", lines[1].strip())
    if m is None:
        print(f'Invalid timestamp in header of "{config.path}"', file=sys.stderr)
        return False

    if abs(int(os.path.getmtime(config.path)) - int(m.group(1))) > 5:
        print(
            f'"{config.path}" appears to have been modified after generation. '
            f"Please make your changes in {config.path.name}.base instead and regenerate "
            "the configuration file.",
            file=sys.stderr,
        )
        return False

    return True


def cmd_doctor(args: argparse.Namespace) -> int:
    if not args.config_path.is_file():
        print(f"Configuration file {args.config_path} doesn't exist. Creating.")
        with args.config_path.open("w") as f:
            f.write("{}\n")

    config = Config(args.config_path)
    unsupported_plugins = set(config.enabled) - SUPPORTED_PLUGINS
    if unsupported_plugins:
        print(
            "Unsupported plugin(s) enabled: {}".format(
                ", ".join(sorted(unsupported_plugins))
            )
        )
        return 1

    for plugin in SUPPORTED_PLUGINS:
        if plugin not in config.enabled:
            continue

        if not validate_config(plugin):
            return 1

    print("All set to manage Base16 themes!")
    return 0


def get_file(path: str) -> str:
    response = requests.get(path)
    if response.status_code == 404:
        raise PathNotFoundError(f"{path} is not a valid file")
    return response.text


def generate_config(config_name: str, theme_str: str) -> bool:
    config = CONFIG_INFO.get(config_name)
    if config is None:
        print(f"{config_name} is not a supported configuration file", file=sys.stderr)
        return False

    if not validate_config(config_name):
        return False

    output = f"{config.comment} {MAGIC_STRING}\n{config.comment} Generated {int(time.time())}\n"
    base_path = config.path.parent / (config.path.name + ".base")
    try:
        with base_path.open() as f:
            output += f.read()
    except FileNotFoundError:
        print(
            "No {config.path.name}.base file found. Run `base16 doctor` for help.",
            file=sys.stderr,
        )
        return False

    if not output.endswith("\n"):
        output += "\n"

    output += theme_str

    path = config.path
    with path.open("w") as f:
        f.write(output)

    if config.post_process is not None and not config.post_process(config):
        return False

    print(f"{config_name} updated successfully")

    return True


def cmd_install(args: argparse.Namespace, config: Config) -> int:
    for plugin in SUPPORTED_PLUGINS:
        if plugin not in config.enabled:
            continue

        try:
            theme_str = get_file(CONFIG_INFO[plugin].theme_url.format(args.theme))
        except PathNotFoundError:
            print(f"Unable to fetch {plugin} theme for {args.theme}", file=sys.stderr)
            return 1

        if not generate_config(plugin, theme_str):
            return 1

    # TODO(jsvana): somehow run zsh functions in parent. maybe need to just instruct user to reopen shell
    """
    eval "base16_$1"
    eval "_base16 /home/jsvana/.config/base16-shell/scripts/base16-$1.sh $1"
    """
    return 0


def main() -> int:
    args = parse_args()

    if args.cmd == cmd_doctor:
        return cmd_doctor(args)

    try:
        config = Config(args.config_path)
    except FileNotFoundError:
        print(
            f'"{args.config_path}" is not a valid file. Run `base16 doctor`'
            "for setup information.",
            file=sys.stderr,
        )
        return 1

    return args.cmd(args, config)


if __name__ == "__main__":
    sys.exit(main())
