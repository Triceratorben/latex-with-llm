"""GROUP 2 - A corpus of realistic point edits.

What an LLM does most often: change a phrase, a number, a citation key, add
emphasis or a footnote, fix a ref, touch text next to inline math. Each edit
below is run through the same oracle:

  * accept-all  -> file == original with `--old` replaced by `--new`
  * reject-all  -> file == original, byte for byte

Edits are table-driven so one failure does not hide the others. A final test
drafts the whole corpus in ONE turn, as a user dictating many changes would.

Run:  python3 tests/test_rw_02_point_edit_corpus.py
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.latex_draft import main as latex_draft  # noqa: E402

PAPER = r"""\documentclass[11pt,a4paper]{article}

\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{amsmath,amssymb,amsthm}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage[numbers,sort&compress]{natbib}
\usepackage{siunitx}
\usepackage{listings}
\usepackage[hidelinks]{hyperref}

\newtheorem{theorem}{Theorem}
\theoremstyle{definition}
\newtheorem{definition}{Definition}

\newcommand{\ours}{\textsc{Rebalance}}

\title{Rebalancing Urban Bike-Share Networks:\
  A Queueing-Theoretic Study}
\author{Jane Doe\thanks{Corresponding author: jane.doe@example.org} \and
  Jos'e N'u~nez \and Anna M\"uller\
  Department of Urban Planning, Example University}
\date{}

\begin{document}
\maketitle

\begin{abstract}
We study rebalancing in bike-share networks using a
non-stationary queueing (NSQ) approach. Our results show that
the rebalanced flow scales quadratically with the incentive amplitude
and depends sensitively on the station--demand coupling.
\end{abstract}

\section{Introduction}
\label{sec:intro}
Bike-share systems are a promising platform for sustainable urban
mobility~\cite{demaio2009,fishman2014}. Early pilots reported
rebalancing ratios of about 10 at \SI{1}{\per\hour} demand
imbalance~\cite{fishman2014}, while later work achieved ratios
above $10^3$~\citep{raviv2013}. In this work we show that pricing incentives can
drive a net dc flow
even with balanced demand. % TODO: cite the Shaheen reference

Our contributions are threefold:
\begin{itemize}
  \item a general expression for the rebalanced flow in the light-traffic limit;
  \item an efficient implementation in \ours{} that scales linearly with
        the number of docking stations;
  \item a comparison with field data at \SI{20}{\celsius}.
\end{itemize}

\section{Theory}
\label{sec:theory}
We consider a station coupled to two demand centres. The resolvent
of the queueing process at the station is
\begin{equation}
  G^{r}(\lambda) = \left[ \lambda I - Q - \Sigma_L(\lambda) - \Sigma_R(\lambda) \right]^{-1},
  \label{eq:resolvent}
\end{equation}
where $Q$ is the station's generator and $\Sigma_{L,R}$ are the
demand coupling terms. The dock-to-dock (dc) flow follows as
\begin{align}
  T(\lambda) &= \Tr!\left[ \Gamma_L G^{r} \Gamma_R G^{a} \right], \\
  F &= \frac{1}{\tau} \int T(\lambda),[b_L(\lambda) - b_R(\lambda)],\mathrm{d}\lambda .
  \label{eq:flow}
\end{align}

\begin{theorem}\label{thm:quad}
In the light-traffic limit the rebalanced flow is proportional to
the square of the incentive amplitude $A$, i.e.\ $F_{\mathrm{net}} \propto A^2$.
\end{theorem}
\begin{proof}
Expand \cref{eq:flow} to second order in $A$ and use the symmetry of
$T(\lambda)$; the linear term vanishes by demand symmetry.
\end{proof}

The implementation is available at \url{https://example.org/rebalance?ref=a%20b#v2}.
A minimal driver looks as follows:
\begin{lstlisting}[language=Python]
# compute the rebalanced flow
for s in stations:      # 50% of the time is spent here
    F += weight(s) * T(s)
\end{lstlisting}

\section{Results}
\label{sec:results}
\Cref{tab:ratios} lists the rebalancing ratios for three stations, and
\cref{fig:fi} shows the flow--incentive characteristic of the best
performing station.

\begin{table}[ht]
  \centering
  \caption{Rebalancing ratios at \SI{1}{\per\hour}.}
  \label{tab:ratios}
  \begin{tabular}{lcc}
    \toprule
    Station & Ratio & Error (%) \\
    \midrule
    A & 12.3 & 1.2 \\
    B & 45.0 & 2.8 \\
    \bottomrule
  \end{tabular}
\end{table}

\begin{figure}[ht]
  \centering
  \fbox{\rule{0pt}{2cm}\rule{4cm}{0pt}}
  \caption{Flow--incentive characteristic of station B.}
  \label{fig:fi}
\end{figure}

The rebalancing ratio increases with the station distance, which agrees
with the trend reported in earlier work~\cite{raviv2013}. The error bars
are dominated by the uncertainty in the demand coupling (about 10\%).

\section{Conclusion}
\label{sec:conclusion}
We have shown that rebalancing in bike-share networks can be
described within the NSQ formalism, and we expect the effect to be
observable in year-round operation.

\bibliographystyle{plainnat}
\bibliography{refs}

\end{document}
"""

# name: (old, new)
EDITS = {
    "hyphen removed, wording changed": (
        "non-stationary queueing (NSQ) approach",
        "nonstationary queueing (NSQ) method"),
    "word swap across a short phrase": ("scales quadratically", "grows quadratically"),
    "en dash to em dash": ("station--demand coupling", "station---demand coupling"),
    "add a key to an existing cite": (
        "mobility~\\cite{demaio2009,fishman2014}",
        "mobility~\\cite{demaio2009,fishman2014,raviv2013}"),
    "strike a tilde-attached cite": ("imbalance~\\cite{fishman2014}", "imbalance"),
    "change a power inside inline math": ("above $10^3$~\\citep{raviv2013}", "above $10^4$~\\citep{raviv2013}"),
    "change the number inside an \\SI line": ("about 10 at \\SI{1}{\\per\\hour}", "about 10 at \\SI{0.5}{\\per\\hour}"),
    "add emphasis": ("net dc flow", "net \\emph{dc} flow"),
    "word inside an item that has a macro": ("that scales linearly", "that scales strictly linearly"),
    "expand a unit phrase": ("at \\SI{20}{\\celsius}", "at mild weather (\\SI{20}{\\celsius})"),
    "text right after \\Cref": ("\\Cref{tab:ratios} lists", "\\Cref{tab:ratios} summarizes"),
    "text right after \\cref": ("\\cref{fig:fi} shows", "\\cref{fig:fi} displays"),
    "strike a cite across a wrap": (
        "agrees\nwith the trend reported in earlier work~\\cite{raviv2013}",
        "agrees\nwith the trend reported in earlier work"),
    "escaped percent in parentheses": (("(about 10\\%)", "(about 15\\%)")),
    "sentence before a line that holds a URL": (
        "The implementation is available at", "The implementation is publicly available at"),
    "word before a list": ("Our contributions are threefold:", "Our contributions are fourfold:"),
    "add a footnote containing a cite": (
        "NSQ formalism", "NSQ formalism\\footnote{See Ref.~\\cite{fishman2014}.}"),
    "insert a word in a plain sentence": ("We consider a station", "We consider a single station"),
    "text beside inline math": (
        "where $Q$ is the station's generator", "where $Q$ is the station's generator matrix"),
    "math with subscripts and a wrap": (
        "$\\Sigma_{L,R}$ are the\ndemand coupling terms",
        "$\\Sigma_{L,R}$ are the\ndemand coupling matrices"),
    "theorem statement with 'i.e.\\ '": (
        "the square of the incentive amplitude $A$, i.e.\\ $F_{\\mathrm{net}} \\propto A^2$",
        "the square of the incentive amplitude $A$, i.e.\\ $F_{\\mathrm{net}} \\propto A^{2}$"),
    "sentence inside a proof": (
        "the linear term vanishes by demand symmetry",
        "the linear term vanishes because of demand symmetry"),
    "section title": ("\\section{Results}", "\\section{Results and discussion}"),
    "added acronym": ("Early pilots", "Early dockless pilots"),
    "typographic quotes": ("a promising platform for sustainable", "a ``promising'' platform for sustainable"),
    "emphasis at a sentence start": ("Our results show", "\\emph{Our} results show"),
    "add a cite before a trailing comment": ("even with balanced demand.", "even with balanced demand~\\citep{shaheen2010}."),
}


class PaperCase(unittest.TestCase):
    """A temp project on disk with the realistic paper plus its .bib file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "project"
        self.dir.mkdir()
        (self.dir / "paper.tex").write_text(PAPER, encoding="utf-8")
        self.addCleanup(os.chmod, self.dir / "paper.tex", 0o644)

    def read(self, name="paper.tex"):
        return (self.dir / name).read_text(encoding="utf-8")

    def cli(self, *argv, main_file="paper.tex"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = latex_draft([str(self.dir / main_file), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def draft_id(self):
        return re.search(r"drafted .*?\b([0-9a-f]{8})\b", self.out).group(1)


def _all_edits_source():
    """The source after every edit in EDITS has been applied in table order."""
    src = PAPER
    for old, new in EDITS.values():
        src = src.replace(old, new, 1)
    return src


# Edits the spec allows the tool to refuse cleanly (the markup cannot carry
# them; the refusal must say so and point at the fallback). Every other edit
# in the corpus must draft.
REFUSAL_OK = {"section title"}


class WholeCorpusInOneTurn(PaperCase):
    def draft_all(self):
        """Draft every edit; return (applied, unexpected) where `unexpected`
        holds only refusals the scenarios do not allow. A clean, explained
        refusal for a context the markup cannot carry (REFUSAL_OK) is a valid
        outcome, not a failure."""
        applied, unexpected = [], []
        for name, (old, new) in EDITS.items():
            rc = self.cli("draft-replace", "--old", old, "--new", new)
            if rc == 0:
                applied.append((name, old, new, self.draft_id()))
            elif name in REFUSAL_OK and (
                "--unmarked" in self.err or "sectioning" in self.err
            ):
                continue  # clean refusal with guidance: allowed
            else:
                unexpected.append((name, self.err.strip()))
        return applied, unexpected

    def test_fixture_edits_are_unique_in_the_paper(self):
        for old, _new in EDITS.values():
            self.assertEqual(PAPER.count(old), 1, f"--old not unique: {old!r}")

    def test_every_edit_in_the_corpus_can_be_drafted_in_one_turn(self):
        applied, unexpected = self.draft_all()
        self.assertEqual(unexpected, [], "edits refused when drafted together")
        self.assertEqual(
            len(applied), len(EDITS) - len(REFUSAL_OK),
            "edits missing without a clean refusal")

    def test_accept_all_equals_the_sequentially_edited_source(self):
        applied, unexpected = self.draft_all()
        self.assertEqual(unexpected, [])
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        want = PAPER
        for _name, old, new, _did in applied:
            want = want.replace(old, new, 1)
        from rw_support import plain_view
        self.assertEqual(plain_view(self.read()), want)

    def test_reject_all_is_byte_identical(self):
        self.draft_all()
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        self.assertEqual(self.read(), PAPER)


if __name__ == "__main__":
    unittest.main()
