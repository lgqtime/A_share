"""Plot normalized and raw total-return matrices from optimizer sweep output."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DEFAULT_INPUT = Path(
    "optimizer_lookback_sweep/20260810_175119/收益率矩阵.csv"
)


def load_matrix(csv_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load total returns and normalize every row by its lookback-day label."""
    frame = pd.read_csv(csv_path, encoding="utf-8-sig")
    if frame.shape[1] < 2:
        raise ValueError("收益率矩阵至少需要一列交易窗口和一列日期数据。")

    windows = pd.to_numeric(frame.iloc[:, 0], errors="raise").astype(int)
    raw_matrix = frame.iloc[:, 1:].apply(pd.to_numeric, errors="coerce")
    raw_matrix.index = windows
    raw_matrix.index.name = "交易窗口（天）"
    normalized_matrix = raw_matrix.div(raw_matrix.index, axis=0)
    return raw_matrix, normalized_matrix


def _configure_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Microsoft YaHei", "Arial", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.linewidth": 0.8,
            "font.size": 9,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )


def _save_figure(fig: plt.Figure, output_dir: Path, stem: str, dpi: int) -> list[Path]:
    paths = [
        output_dir / f"{stem}.png",
        output_dir / f"{stem}.svg",
        output_dir / f"{stem}.pdf",
    ]
    fig.savefig(paths[0], dpi=dpi, bbox_inches="tight", facecolor="white")
    fig.savefig(paths[1], bbox_inches="tight", facecolor="white")
    fig.savefig(paths[2], bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return paths


def plot_normalized_heatmap(
    normalized_matrix: pd.DataFrame,
    output_dir: Path,
    dpi: int,
) -> list[Path]:
    figure, axis = plt.subplots(figsize=(14, 8.5))
    colormap = plt.colormaps["YlGnBu"].copy()
    colormap.set_bad("#e5e7eb")
    image = axis.imshow(
        np.ma.masked_invalid(normalized_matrix.to_numpy(dtype=float)),
        aspect="auto",
        cmap=colormap,
    )
    axis.set_xticks(range(len(normalized_matrix.columns)))
    axis.set_xticklabels(normalized_matrix.columns, rotation=45, ha="right", fontsize=8)
    axis.set_yticks(range(len(normalized_matrix.index)))
    axis.set_yticklabels(normalized_matrix.index, fontsize=8)
    axis.set_xlabel("截止交易日")
    axis.set_ylabel("交易窗口（天）")
    axis.set_title("归一化总收益率热力图", pad=12, weight="bold")
    colorbar = figure.colorbar(image, ax=axis, pad=0.02)
    colorbar.set_label("总收益率 / 交易窗口（% / 天）")
    figure.tight_layout()
    return _save_figure(figure, output_dir, "normalized_return_heatmap", dpi)


def plot_return_points(
    matrix: pd.DataFrame,
    output_dir: Path,
    stem: str,
    title: str,
    y_label: str,
    dpi: int,
) -> list[Path]:
    figure, axis = plt.subplots(figsize=(14, 7.5))
    colors = plt.colormaps["tab20"].colors
    for index, column in enumerate(matrix.columns):
        axis.scatter(
            matrix.index,
            matrix[column],
            s=26,
            color=colors[index % len(colors)],
            alpha=0.9,
            label=column,
            linewidths=0,
        )
    axis.set_xticks(matrix.index)
    axis.set_xlabel("交易窗口（天）")
    axis.set_ylabel(y_label)
    axis.set_title(title, pad=12, weight="bold")
    axis.grid(axis="y", color="#d1d5db", linewidth=0.7, alpha=0.8)
    axis.set_axisbelow(True)
    axis.legend(
        title="截止交易日",
        bbox_to_anchor=(1.01, 1),
        loc="upper left",
        borderaxespad=0,
        fontsize=7,
        title_fontsize=8,
        markerscale=1.1,
    )
    figure.tight_layout()
    return _save_figure(figure, output_dir, stem, dpi)


def render_plots(csv_path: Path, output_dir: Path, dpi: int = 300) -> list[Path]:
    if dpi < 72:
        raise ValueError("dpi 至少为 72。")
    raw_matrix, normalized_matrix = load_matrix(csv_path)
    # # print("实际列名列表：", raw_matrix.columns.tolist())  
    selected_columns = ['2026-08-07', '2026-08-06', '2026-08-05', '2026-08-04', '2026-08-03', '2026-07-31', '2026-07-30', '2026-07-29', '2026-07-28', '2026-07-27', '2026-07-24',]   # 以实际日期列名为准
    raw_matrix = raw_matrix[selected_columns]
    normalized_matrix = normalized_matrix[selected_columns]
    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_style()
    paths = plot_normalized_heatmap(normalized_matrix, output_dir, dpi)
    paths.extend(
        plot_return_points(
            normalized_matrix,
            output_dir,
            "normalized_return_points",
            "归一化总收益率点图",
            "总收益率 / 交易窗口（% / 天）",
            dpi,
        )
    )
    paths.extend(
        plot_return_points(
            raw_matrix,
            output_dir,
            "raw_return_points",
            "原始总收益率点图",
            "总收益率（%）",
            dpi,
        )
    )
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制优化器收益率矩阵图。")
    parser.add_argument("--input-csv", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    csv_path = args.input_csv.resolve()
    if not csv_path.is_file():
        print(f"未找到收益率矩阵：{csv_path}")
        return 1
    output_dir = args.output_dir or csv_path.parent / "plots"
    for path in render_plots(csv_path, output_dir, args.dpi):
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
