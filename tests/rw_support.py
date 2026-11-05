"""Shared fixture for the real-world scenario suites (test_rw_*.py)."""

from __future__ import annotations

import contextlib
import sys
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _kpse(name):
    exe = shutil.which("kpsewhich")
    return bool(exe and subprocess.run([exe, name], capture_output=True, text=True).stdout.strip())


HAVE_TEX = (
    not os.environ.get("LATEX_DRAFT_TESTS_NO_TEX")
    and bool(shutil.which("latexmk") or shutil.which("pdflatex"))
    and all(_kpse(f) for f in ("changes.sty", "ulem.sty", "todonotes.sty"))
)
needs_tex = unittest.skipUnless(HAVE_TEX, "needs latexmk/pdflatex, pdftotext, changes, ulem, todonotes")

# pdftotext prints T1 ligatures as control characters
LIGATURES = {"\x1b": "ff", "\x1c": "fi", "\x1d": "fl", "\x1e": "ffi", "\x1f": "ffl",
             "\ufb01": "fi", "\ufb02": "fl"}


def pdf_text(pdf):
    out = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
    for k, v in LIGATURES.items():
        out = out.replace(k, v)
    return " ".join(out.split())


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

BIB = r"""@article{demaio2009,
  author  = {DeMaio, Paul},
  title   = {Bike-sharing: History, impacts, models of provision, and future},
  journal = {Journal of Public Transportation},
  year    = {2009}, volume = {12}, pages = {41--56}
}
@article{fishman2014,
  author  = {Fishman, Elliot and Washington, Simon and Haworth, Narelle},
  title   = {Bike share: a synthesis of the literature},
  journal = {Transport Reviews},
  year    = {2014}, volume = {34}, pages = {148--166}
}
@article{raviv2013,
  author  = {Raviv, Tal and Tzur, Michal and Forma, Iris A.},
  title   = {Static repositioning in a bike-sharing system},
  journal = {IIE Transactions},
  year    = {2013}, volume = {45}, pages = {1--14}
}
@article{shaheen2010,
  author  = {Shaheen, Susan A. and Guzman, Stacey and Zhang, Hinda},
  title   = {Bikesharing in Europe, the Americas, and Asia},
  journal = {Transportation Research Record},
  year    = {2010}, volume = {2143}, pages = {159--167}
}
"""


sys.path.insert(0, str(ROOT))
from tools.latex_draft import (  # noqa: E402
    _clean_source,
    main as latex_draft,
)


def plain_view(text: str) -> str:
    """The document with every tool trace removed (the finalize view).

    Accepted text kept marked (`keep_markup` default) is unwrapped, so the
    accept oracle compares plain prose exactly as the pre-markup scenarios
    specified.
    """
    return _clean_source(text, None)[0]


class PaperCase(unittest.TestCase):
    """A temp project holding the realistic paper plus its .bib file.

    `cli()` drives the tool in-process: `self.out`/`self.err` hold the output
    of the last command, `self.rc` its exit code.
    """

    files = {"paper.tex": PAPER, "refs.bib": BIB}

    # Four point edits on one long line (group 1): they must compose into a
    # single numbered draft block resolved as a unit.
    EDITS = [
        ("about 10 at", "roughly 10 at"),
        ("$10^3$", "$10^4$"),
        ("net dc flow", "net \\emph{dc} flow"),
        ("promising platform", "promising and versatile platform"),
    ]
    main_name = "paper.tex"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "project"
        self.dir.mkdir()
        for name, content in self.files.items():
            self.write(name, content)

    def write(self, name, content):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def read(self, name=None):
        # bytes -> decode: no universal-newline translation, so a CRLF file
        # is compared byte-exactly (read_text would fold CRLF to LF and make
        # every CRLF oracle compare the wrong bytes).
        return (self.dir / (name or self.main_name)).read_bytes().decode("utf-8")

    def reset(self):
        """Restore every fixture file and remove state, drafts and build artifacts."""
        for p in self.dir.iterdir():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        for name, content in self.files.items():
            self.write(name, content)

    def cli(self, *argv, main_file=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.rc = latex_draft([str(self.dir / (main_file or self.main_name)), *argv])
        self.out, self.err = out.getvalue(), err.getvalue()
        return self.rc

    def use(self, doc):
        """Replace the main file's content with `doc`."""
        self.write(self.main_name, doc)

    def compile(self, main=None):
        """Compile with the tool's own `compile`; return the PDF text."""
        argv = ["compile"]
        if main is not None:
            argv.append(main)
        self.assertEqual(self.cli(*argv), 0, "compile failed:\n" + (self.err + self.out)[-2500:])
        return pdf_text(self.dir / (main or self.main_name).with_suffix(".pdf"))

    def draft_id(self):
        m = re.search(r"drafted .*?\b([0-9a-f]{8})\b", self.out)
        self.assertIsNotNone(m, "no draft id in the tool's output:\n" + self.out)
        return m.group(1)

    def round_trip(self, old, new, doc=None, eol="\n"):
        """The accept/reject oracle.

        On a fresh copy: `draft-replace old -> new`, `accept --all`, and the
        file must equal the original with exactly `old` replaced by `new`
        (in the plain, finalize view - accepted text may carry provenance
        markup).  On another fresh copy: `reject --all` must restore the
        original bytes exactly.
        """
        if doc is not None:
            self.use(doc)
        snapshot = {name: self.read(name) for name in self.files}
        original = snapshot[self.main_name]
        want = original.replace(old.replace("\n", eol), new.replace("\n", eol), 1)
        self.assertEqual(self.cli("draft-replace", "--old", old, "--new", new), 0, self.err)
        self.assertEqual(self.cli("accept", "--all"), 0, self.err)
        self.assertEqual(plain_view(self.read()), want, "accepted text differs from the oracle")
        self.reset()
        for name, content in snapshot.items():
            self.write(name, content)
        self.assertEqual(self.cli("draft-replace", "--old", old, "--new", new), 0, self.err)
        self.assertEqual(self.cli("reject", "--all"), 0, self.err)
        self.assertEqual(self.read(), original, "reject-all did not restore the original bytes")
