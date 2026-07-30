"""Load the canonical strategy implementation used by daily prediction.

The historical backtest helpers live under ``strategy_backtest`` while the
production screening implementation lives at the project root.  Loading it
through this module prevents the optimizer from silently using the older UI
snapshot in this directory.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


PROJECT_DIR = Path(__file__).resolve().parent.parent
ROOT_STRATEGY_PATH = PROJECT_DIR / "szse_quant_app.py"
_FALLBACK_MODULE_NAME = "_szse_quant_production_strategy"


def _module_path(module: object) -> Path | None:
    source_path = getattr(module, "__file__", None)
    return Path(source_path).resolve() if source_path else None


def _load_production_strategy() -> ModuleType:
    """Load the root strategy even if a legacy same-named module is present."""

    existing = sys.modules.get("szse_quant_app")
    if existing is not None and _module_path(existing) == ROOT_STRATEGY_PATH.resolve():
        return existing

    cached = sys.modules.get(_FALLBACK_MODULE_NAME)
    if cached is not None and _module_path(cached) == ROOT_STRATEGY_PATH.resolve():
        return cached

    spec = importlib.util.spec_from_file_location(_FALLBACK_MODULE_NAME, ROOT_STRATEGY_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载根目录策略实现：{ROOT_STRATEGY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_FALLBACK_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_FALLBACK_MODULE_NAME, None)
        raise
    return module


strategy_app = _load_production_strategy()
