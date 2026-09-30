#!/usr/bin/env python3

"""Full-screen terminal interface for browsing and editing weather settings."""

import argparse
import json
import math
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PATH = PROJECT_ROOT / ".venv"

try:
    import textual
except ModuleNotFoundError as error:
    if error.name != "textual":
        raise
    venv_python = VENV_PATH / "bin" / "python"
    if venv_python.is_file() and Path(sys.prefix).resolve() != VENV_PATH.resolve():
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit(
        "Textual is required. Install it with: "
        f"{venv_python} -m pip install -r {PROJECT_ROOT / 'requirements-settings-gr.txt'}"
    )

import settings
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, Select, Static


class SettingsApp(App):
    TITLE = "WeatherHAT Settings"
    SUB_TITLE = "Browse and update runtime configuration"

    CSS = """
    Screen {
        background: #10191b;
        color: #e7f0ed;
    }
    #heading {
        color: #f0aa72;
        text-style: bold;
        padding: 1 1 0 1;
        height: 3;
    }
    #workspace {
        height: 1fr;
        padding: 0 1 1 1;
    }
    #setting-list {
        width: 2fr;
        min-width: 30;
        height: 1fr;
        margin-right: 1;
    }
    #filter {
        margin-bottom: 1;
    }
    #settings-table {
        height: 1fr;
        border: round #397f78;
    }
    #editor {
        width: 1fr;
        min-width: 28;
        height: 1fr;
        padding: 1;
        border: round #34494a;
    }
    #selected-path {
        height: auto;
        min-height: 3;
        color: #f0aa72;
        text-style: bold;
        margin-bottom: 1;
    }
    #value-label {
        height: 2;
        color: #b7c8c3;
    }
    #new-value {
        margin-bottom: 1;
    }
    #boolean-value, #choice-value, #number-value, #integer-controls {
        margin-bottom: 1;
    }
    #integer-controls {
        grid-size: 3 1;
        grid-columns: 5 1fr 5;
        grid-gutter: 0 1;
        height: 3;
    }
    #integer-value {
        width: 1fr;
        height: 3;
        content-align: center middle;
        border: round #397f78;
        color: #f0aa72;
        text-style: bold;
    }
    #integer-controls Button {
        width: 5;
        margin: 0;
    }
    #value-hint {
        height: auto;
        color: #91a9a3;
        margin-bottom: 1;
    }
    #editor-actions {
        height: 3;
    }
    #editor-actions Button {
        margin-right: 1;
    }
    #status {
        height: auto;
        min-height: 3;
        padding: 0 1 1 1;
        color: #b7c8c3;
    }
    """

    BINDINGS = [
        ("ctrl+s", "apply_change", "Apply value"),
        ("ctrl+r", "reload_settings", "Reload settings"),
    ]

    def __init__(self, config_path=None):
        super().__init__()
        self.config_path = Path(config_path or settings.DEFAULT_SETTINGS_PATH)
        self.config = {}
        self.leaf_values = {}
        self.selected_path = None
        self.rebuilding_table = False
        self.editor_kind = None
        self.integer_value = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("SETTINGS WORKBENCH", id="heading")
        with Horizontal(id="workspace"):
            with Vertical(id="setting-list"):
                yield Input(placeholder="Filter settings...", id="filter")
                yield DataTable(id="settings-table", cursor_type="row", zebra_stripes=True)
            with Container(id="editor"):
                yield Static("Select a setting", id="selected-path")
                yield Label("New value", id="value-label")
                yield Checkbox("Enabled", id="boolean-value")
                yield Select([], prompt="Choose a value", id="choice-value")
                with Grid(id="integer-controls"):
                    yield Button("-", id="step-down")
                    yield Static("0", id="integer-value")
                    yield Button("+", id="step-up")
                yield Input(placeholder="Number", type="number", id="number-value")
                yield Input(placeholder="JSON value or text", id="new-value")
                yield Static(
                    "Choice lists come from the related available_* setting. "
                    "Use the + and - buttons to step through integers.",
                    id="value-hint",
                )
                with Horizontal(id="editor-actions"):
                    yield Button("Apply change", id="apply", variant="primary", disabled=True)
                    yield Button("Reload", id="reload")
        yield Static("Select a row, edit its value, then apply the change.", id="status")
        yield Footer()

    def on_mount(self):
        self.query_one("#settings-table", DataTable).add_columns("Group", "Setting", "Current value")
        for selector in ("#boolean-value", "#choice-value", "#integer-controls", "#number-value", "#new-value"):
            self.query_one(selector).display = False
        self.reload_settings()

    def set_status(self, message):
        self.query_one("#status", Static).update(message)

    def reload_settings(self):
        try:
            self.config = settings.load_config(self.config_path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            self.set_status(f"Could not load settings: {error}")
            return
        self.refresh_table(self.selected_path)
        self.set_status(f"Loaded {self.config_path}")

    def refresh_table(self, preferred_path=None):
        self.leaf_values = dict(settings.iter_leaves(self.config))
        filter_text = self.query_one("#filter", Input).value.strip().casefold()
        paths = [
            path for path in sorted(self.leaf_values)
            if filter_text in path.casefold()
            and not path.rsplit(".", 1)[-1].startswith("available_")
        ]

        table = self.query_one("#settings-table", DataTable)
        self.rebuilding_table = True
        table.clear()
        for path in paths:
            group, separator, setting_name = path.partition(".")
            if not separator:
                setting_name = group
            table.add_row(group, setting_name, json.dumps(self.leaf_values[path]), key=path)
        self.rebuilding_table = False

        selected_path = preferred_path if preferred_path in paths else (paths[0] if paths else None)
        if selected_path is None:
            self.selected_path = None
            self.query_one("#selected-path", Static).update("No matching settings")
            self.hide_editors()
            self.query_one("#apply", Button).disabled = True
            return

        self.show_setting(selected_path)
        row_index = paths.index(selected_path)
        table.move_cursor(row=row_index, column=0)

    def show_setting(self, path):
        self.selected_path = path
        self.query_one("#selected-path", Static).update(path)
        value = self.leaf_values[path]
        choices = self.choice_values(path)
        self.hide_editors()

        if isinstance(value, bool):
            self.editor_kind = "boolean"
            self.query_one("#value-label", Label).update("Boolean")
            self.query_one("#boolean-value", Checkbox).value = value
            self.query_one("#boolean-value").display = True
        elif choices is not None:
            self.editor_kind = "choice"
            self.query_one("#value-label", Label).update("Choose an allowed value")
            choice_input = self.query_one("#choice-value", Select)
            choice_input.set_options([(str(option), option) for option in choices])
            choice_input.value = value
            choice_input.display = True
        elif isinstance(value, int):
            self.editor_kind = "integer"
            self.query_one("#value-label", Label).update("Integer")
            self.integer_value = value
            self.query_one("#integer-value", Static).update(str(value))
            self.query_one("#integer-controls").display = True
        elif isinstance(value, float):
            self.editor_kind = "number"
            self.query_one("#value-label", Label).update("Number")
            self.query_one("#number-value", Input).value = str(value)
            self.query_one("#number-value").display = True
        else:
            self.editor_kind = "text"
            self.query_one("#value-label", Label).update("JSON value or text")
            self.query_one("#new-value", Input).value = json.dumps(value)
            self.query_one("#new-value").display = True
        self.query_one("#apply", Button).disabled = False

    def choice_values(self, path, config=None):
        choice_sources = {
            "degree": "available_degrees",
            "window_hours": "available_window_hours",
        }
        setting_name = path.rsplit(".", 1)[-1]
        source_name = choice_sources.get(setting_name)
        if source_name is None:
            return None
        parent, _ = settings.resolve_parent(config if config is not None else self.config, path)
        choices = parent.get(source_name)
        if isinstance(choices, list) and choices and self.leaf_values[path] in choices:
            return choices
        return None

    def hide_editors(self):
        for selector in ("#boolean-value", "#choice-value", "#integer-controls", "#number-value", "#new-value"):
            self.query_one(selector).display = False

    def edited_value(self):
        if self.editor_kind == "boolean":
            return self.query_one("#boolean-value", Checkbox).value
        if self.editor_kind == "choice":
            return self.query_one("#choice-value", Select).value
        if self.editor_kind == "integer":
            return self.integer_value
        if self.editor_kind == "number":
            value = float(self.query_one("#number-value", Input).value)
            if not math.isfinite(value):
                raise ValueError("number must be finite")
            return value
        return settings.parse_value(self.query_one("#new-value", Input).value)

    @on(Input.Changed, "#filter")
    def filter_settings(self, event: Input.Changed):
        self.refresh_table(self.selected_path)

    @on(DataTable.RowHighlighted, "#settings-table")
    def select_setting(self, event: DataTable.RowHighlighted):
        if self.rebuilding_table or event.row_key is None:
            return
        path = event.row_key.value
        if path in self.leaf_values:
            self.show_setting(path)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "apply":
            self.action_apply_change()
        elif event.button.id == "reload":
            self.reload_settings()
        elif event.button.id == "step-down":
            self.step_integer(-1)
        elif event.button.id == "step-up":
            self.step_integer(1)

    def step_integer(self, amount):
        if self.editor_kind != "integer":
            return
        value = self.integer_value + amount
        if self.selected_path == "sampling.cpu_temperature_samples_to_average":
            value = max(1, value)
        self.integer_value = value
        self.query_one("#integer-value", Static).update(str(value))

    def action_apply_change(self):
        if self.selected_path is None:
            return
        try:
            config = settings.load_config(self.config_path)
            parent, key = settings.resolve_parent(config, self.selected_path)
            current_value = parent[key]
            new_value = self.edited_value()
            settings.validate_value(config, self.selected_path, current_value, new_value)
            choices = self.choice_values(self.selected_path, config)
            if choices is not None and new_value not in choices:
                raise ValueError(f"Choose one of the available values: {choices}")
            parent[key] = new_value
            self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            self.set_status(f"Change not applied: {error}")
            return

        self.config = config
        self.refresh_table(self.selected_path)
        self.set_status(f"Updated {self.selected_path} in {self.config_path}")

    def action_reload_settings(self):
        self.reload_settings()


def build_parser():
    parser = argparse.ArgumentParser(description="Interactive terminal UI for weather settings.")
    parser.add_argument("--config", type=Path, default=settings.DEFAULT_SETTINGS_PATH,
                        help="Settings JSON path (default: %(default)s)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    SettingsApp(args.config).run()


if __name__ == "__main__":
    main()