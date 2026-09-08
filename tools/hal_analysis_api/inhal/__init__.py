"""The in-HAL side of the API: the only code here that imports ``hal_py``.

``query_script.py`` is executed by ``hal --python-script``; it reads the request
named by ``$HAL_ANALYSIS_QUERY``, puts the tools directory on ``sys.path`` and
hands over to :mod:`hal_analysis_api.inhal.dispatch`, which loads the netlist
through :mod:`hal_viz.halenv` and answers with
:mod:`hal_analysis_api.netlist_query`.

The split matters: everything that decides *what* an answer looks like lives
outside HAL and is unit tested on a machine that cannot build HAL, and
everything in here is loading, calling and serializing.
"""

__all__ = []
