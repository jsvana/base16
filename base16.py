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
from typing import Any, Callable, Optional, Iterable

import requests


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

    subparsers = parser.add_subparsers(dest='cmd')
    subparsers.required = True

    doctor_parser = subparsers.add_parser(
        "doctor", help="Check your base16 setup for proper configuration"
    )
    doctor_parser.set_defaults(cmd=cmd_doctor)

    install_parser = subparsers.add_parser(
        "install", parents=[theme_arg], help="Install the given Base16 theme"
    )
    install_parser.set_defaults(cmd=cmd_install)

    list_parser = subparsers.add_parser(
        "list", help="List all installed themes"
    )
    list_parser.set_defaults(cmd=cmd_list)

    show_parser = subparsers.add_parser(
        "show", help="Show currently installed theme"
    )
    show_parser.set_defaults(cmd=cmd_show)

    return parser.parse_args()


class PluginInfo:
    def __init__(
        self,
        name: str,
        path_in_home: Path,
    ) -> None:
        self.name = name
        self.path = Path.home() / path_in_home

    def validate(self) -> bool:
        raise NotImplementedError()

    def install(self, theme: str) -> bool:
        raise NotImplementedError()


class DownloadedPluginInfo(PluginInfo):

    MAGIC_STRING = "Written by base16 manager, do not modify manually"

    def __init__(
        self,
        name: str,
        path_in_home: Path,
        comment: str,
        theme_url: str,
        post_process_func: Optional[Callable[["PluginInfo", str], bool]] = None,
    ) -> None:
        super().__init__(name, path_in_home)
        self.comment = comment
        self.theme_url = theme_url
        self.post_process_func = post_process_func

    def install(self, theme: str) -> bool:
        try:
            theme_str = self.get_file(theme)
        except PathNotFoundError:
            print(f"Unable to fetch {self.name} theme for {theme}", file=sys.stderr)
            return False

        if not self.generate(theme_str):
            return False

        return True

    def validate(self) -> bool:
        base_path = self.path.parent / (self.path.name + ".base")
        if not self.path.is_file():
            if not base_path.is_file():
                print(
                    "No {self.name} or {self.name}.base file exist. Please create "
                    f"a base configuration at {base_path}. base16 take that and "
                    "concatenate it with the downloaded theme file.",
                    file=sys.stderr,
                )
                return False
            return True

        with self.path.open() as f:
            lines = f.readlines()

        if not lines:
            return True

        if len(lines) < 2:
            print(f'"{self.path}" has an invalid header', file=sys.stderr)
            return False

        if lines[0].strip() != f"{self.comment} {self.MAGIC_STRING}":
            print(
                f'"{self.path}" does not appear to be managed by base16. Move your '
                f'existing {self.path.name} file to "{self.path.name}.base" and re-run, and '
                "base16 will concatenate that file with the downloaded theme file",
                file=sys.stderr,
            )
            return False

        m = re.match(f"{self.comment} Generated (\d+)$", lines[1].strip())
        if m is None:
            print(f'Invalid timestamp in header of "{self.path}"', file=sys.stderr)
            return False

        if abs(int(os.path.getmtime(self.path)) - int(m.group(1))) > 5:
            print(
                f'"{self.path}" appears to have been modified after generation. '
                f"Please make your changes in {self.path.name}.base instead and regenerate "
                "the configuration file.",
                file=sys.stderr,
            )
            return False

        return True

    def get_file(self, theme: str) -> str:
        response = requests.get(self.theme_url.format(theme))
        if response.status_code == 404:
            raise PathNotFoundError(f"{path} is not a valid file")
        return response.text

    def generate(self, theme_str: str) -> bool:
        if not self.validate():
            return False

        output = f"{self.comment} {self.MAGIC_STRING}\n{self.comment} Generated {int(time.time())}\n"
        base_path = self.path.parent / (self.path.name + ".base")
        try:
            with base_path.open() as f:
                output += f.read()
        except FileNotFoundError:
            print(
                "No {self.path.name}.base file found. Run `base16 doctor` for help.",
                file=sys.stderr,
            )
            return False

        if not output.endswith("\n"):
            output += "\n"

        output += theme_str

        path = self.path
        with path.open("w") as f:
            f.write(output)

        if self.post_process_func is not None and not self.post_process_func(self):
            return False

        return True


class ShellPluginInfo(PluginInfo):

    SCRIPT_NAME_PATTERN = re.compile('base16-(.*)\.sh$')

    def __init__(self):
        super().__init__('shell', Path('.config/base16-shell'))

    @property
    def available_themes(self) -> Iterable[str]:
        for script in (self.path / 'scripts').glob('*.sh'):
            m = self.SCRIPT_NAME_PATTERN.match(script.name)
            if m is None:
                continue
            yield m.group(1)

    @property
    def current_theme(self):
        m = self.SCRIPT_NAME_PATTERN.match((Path.home() / '.base16_theme').resolve().name)
        if m is None:
            return None
        return m.group(1)

    def validate(self) -> bool:
        if not self.path.is_dir():
            print(
                "You don't appear to be using base16_shell. Clone "
                "https://github.com/chriskempson/base16-shell into "
                "~/.config/base16-shell and follow the rest of the "
                "installation instructions on the page, and then re-"
                "run `base16 doctor`.",
                file=sys.stderr,
            )
            return False

        if 'BASE16_THEME' not in os.environ:
            print(
                "You don't appear to be using base16_shell. Ensure "
                "that you've followed the shell setup steps at "
                "https://github.com/chriskempson/base16-shell and then "
                "re-run `base16 doctor`.",
                file=sys.stderr,
            )
            return False

        try:
            os.stat(Path.home() / '.base16_theme')
        except FileNotFoundError:
            print(
                "~/.base16_theme currently isn't linked to an installed theme. "
                "Reinstall the theme with `base16 install <theme>`.",
                file=sys.stderr,
            )
            return False

        return True

    def install(self, theme: str) -> bool:
        theme_path = self.path / f'scripts/base16-{theme}.sh'
        if not theme_path.is_file():
            print(
                f"{theme_path} isn't a valid theme file. Installed themes: "
                "{}".format(', '.join(list(self.available_themes))),
                file=sys.stderr,
            )
            return False

        destination_path = Path.home() / '.base16_theme'
        if destination_path.is_file() or destination_path.is_symlink():
            destination_path.unlink()

        destination_path.symlink_to(theme_path)

        with (Path.home() / '.vimrc_background').open('w') as f:
            lines = [
                f"if !exists('g:colors_name') || g:colors_name != 'base16-{theme}'\n",
                f"  colorscheme base16-{theme}\n",
                "endif\n",
            ]
            f.write(''.join(lines))

        return True


def sync_xresources(plugin_info: DownloadedPluginInfo) -> bool:
    proc = subprocess.run(["xrdb", "-merge", plugin_info.path])
    if proc.returncode != 0:
        print("Error running xrdb", file=sys.stderr)
        return False
    return True


SUPPORTED_PLUGINS = {
    "shell": ShellPluginInfo(),
    "xresources": DownloadedPluginInfo(
        "xresources",
        Path(".Xresources"),
        "!",
        "https://raw.githubusercontent.com/chriskempson/base16-xresources/master/xresources/base16-{}.Xresources",
        post_process_func=sync_xresources,
    ),
    "dunst": DownloadedPluginInfo(
        "dunst",
        Path(".config/dunst/dunstrc"),
        "#",
        "https://raw.githubusercontent.com/khamer/base16-dunst/master/themes/base16-{}.dunstrc",
    ),
    "i3": DownloadedPluginInfo(
        "i3",
        Path(".config/i3/config"),
        "#",
        "https://raw.githubusercontent.com/khamer/base16-i3/master/colors/base16-{}.config",
    ),
}


def cmd_doctor(args: argparse.Namespace) -> int:
    if not args.config_path.is_file():
        print(f"Configuration file {args.config_path} doesn't exist. Creating.")
        with args.config_path.open("w") as f:
            f.write("{}\n")

    config = Config(args.config_path)
    unsupported_plugins = set(config.enabled) - set(SUPPORTED_PLUGINS)
    if unsupported_plugins:
        print(
            "Unsupported plugin(s) enabled: {}".format(
                ", ".join(sorted(unsupported_plugins))
            )
        )
        return 1

    if 'shell' not in config.enabled:
        print(f'Plugin "shell" must be enabled. Please update {args.config_path} accordingly.', file=sys.stderr)
        return 1

    for plugin, plugin_info in SUPPORTED_PLUGINS.items():
        if plugin not in config.enabled:
            continue

        if not plugin_info.validate():
            return 1

    print("All set to manage Base16 themes!")
    return 0


def cmd_install(args: argparse.Namespace, config: Config) -> int:
    for plugin, plugin_info in SUPPORTED_PLUGINS.items():
        if plugin not in config.enabled:
            continue

        config_info = SUPPORTED_PLUGINS[plugin]
        if not config_info.install(args.theme):
            return 1

        print(f"{plugin} installed successfully")

    return 0


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    for theme in ShellPluginInfo().available_themes:
        print(theme)
    return 0


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    theme = ShellPluginInfo().current_theme
    if theme is None:
        print('No theme installed currently. Run `base16 doctor` for help.', file=sys.stderr)
        return 1

    print(theme)
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
