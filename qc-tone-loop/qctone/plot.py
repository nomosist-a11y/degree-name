"""One PNG per iteration that Claude can look at: curves, difference, spectrograms."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from qctone.compare import CURVE_TOLERANCE_DB  # noqa: E402
from qctone.features import THIRD_OCTAVE_CENTRES  # noqa: E402


def _spectrogram(ax, data, sr, title, seconds=8.0):
    mono = data[: int(seconds * sr)].mean(axis=1)
    ax.specgram(mono, NFFT=2048, Fs=sr, noverlap=1536, cmap="magma", vmin=-120)
    ax.set_yscale("symlog", linthresh=500)
    ax.set_ylim(40, 16000)
    ax.set_title(title, fontsize=9)
    ax.set_xlabel("s")
    ax.set_ylabel("Hz")


def comparison_png(path, render_feat, target_feat, cmp, render_audio=None,
                   target_audio=None, title=""):
    centres = np.array(THIRD_OCTAVE_CENTRES)
    rows = 3 if render_audio is not None and target_audio is not None else 2
    fig = plt.figure(figsize=(11, 3.2 * rows), dpi=100)
    grid = fig.add_gridspec(rows, 2)

    ax = fig.add_subplot(grid[0, :])
    ax.semilogx(centres, target_feat["third_octave"]["db"], "o-", label="target", color="#2b6cb0")
    ax.semilogx(centres, render_feat["third_octave"]["db"], "s-", label="render", color="#c05621")
    ax.set_xticks([50, 100, 200, 500, 1000, 2000, 5000, 10000])
    ax.set_xticklabels(["50", "100", "200", "500", "1k", "2k", "5k", "10k"])
    ax.set_ylabel("dB (200 Hz-5 kHz mean = 0)")
    ax.set_title(f"{title}  score {cmp['score']}", fontsize=10)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend()

    ax = fig.add_subplot(grid[1, :])
    diff = np.array(cmp["third_octave_diff_db"])
    colors = ["#c53030" if d > CURVE_TOLERANCE_DB else "#2f855a" if d < -CURVE_TOLERANCE_DB
              else "#a0aec0" for d in diff]
    ax.bar(range(len(diff)), diff, color=colors)
    ax.set_xticks(range(len(diff)))
    ax.set_xticklabels([f"{c / 1000:g}k" if c >= 1000 else str(c) for c in centres],
                       rotation=60, fontsize=7)
    ax.axhspan(-CURVE_TOLERANCE_DB, CURVE_TOLERANCE_DB, color="#e2e8f0", alpha=0.6)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("render - target dB")
    ax.set_title("red = too much (cut), green = too little (boost)", fontsize=9)

    if rows == 3:
        sr = 48000
        _spectrogram(fig.add_subplot(grid[2, 0]), target_audio, sr, "target")
        _spectrogram(fig.add_subplot(grid[2, 1]), render_audio, sr, "render")

    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
