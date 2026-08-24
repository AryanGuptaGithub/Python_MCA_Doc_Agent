"""
Loads config.yaml and resolves every path in it relative to a single,
user-configurable `paths.project_root`. This is the ONLY place path
resolution happens — every other module receives already-resolved
absolute paths and never touches config.yaml directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class AppConfig:
    raw: dict = field(default_factory=dict)

    # resolved absolute paths
    project_root: Path = None
    raw_dir: Path = None
    output_dir: Path = None
    manifest_db: Path = None
    logs_dir: Path = None

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, *keys, default=None):
        """Nested get, e.g. config.get('gemini', 'model')."""
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def _resolve_path(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def load_config(config_path: str | Path = None) -> AppConfig:
    """
    Loads config.yaml (defaults to the file next to the project root),
    loads .env for secrets, resolves all filesystem paths, and creates
    any output directories that don't exist yet.
    """
    # Locate config.yaml: default is two levels up from this file
    # (src/config_loader.py -> project folder / config.yaml)
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. "
            "Copy config.yaml into place or pass --config <path>."
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Load .env from the same folder as config.yaml (if present)
    env_path = config_path.parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    else:
        load_dotenv()  # fall back to any .env on default search path

    paths_cfg = raw.get("paths", {})
    project_root_setting = paths_cfg.get("project_root", ".")
    project_root_raw = Path(project_root_setting)
    # A relative project_root is relative to the config.yaml's own folder,
    # so "." always means "the folder config.yaml lives in" regardless of
    # the current working directory the script is launched from (important
    # for Windows Task Scheduler, which may use a different CWD).
    project_root = (
        project_root_raw
        if project_root_raw.is_absolute()
        else (config_path.parent / project_root_raw)
    ).resolve()

    cfg = AppConfig(raw=raw)
    cfg.project_root = project_root
    cfg.raw_dir = _resolve_path(project_root, paths_cfg.get("raw_dir", "data/raw"))
    cfg.output_dir = _resolve_path(project_root, paths_cfg.get("output_dir", "data/output"))
    cfg.manifest_db = _resolve_path(project_root, paths_cfg.get("manifest_db", "data/manifest.sqlite3"))
    cfg.logs_dir = _resolve_path(project_root, paths_cfg.get("logs_dir", "logs"))

    for d in (cfg.raw_dir, cfg.output_dir, cfg.manifest_db.parent, cfg.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    return cfg


def get_gemini_api_key(cfg: AppConfig) -> str:
    env_var = cfg.get("gemini", "api_key_env_var", default="GEMINI_API_KEY")
    key = os.environ.get(env_var)
    if not key:
        raise RuntimeError(
            f"Gemini API key not found. Set it in a .env file next to config.yaml as:\n"
            f"  {env_var}=your_key_here\n"
            f"(see .env.example)"
        )
    return key
