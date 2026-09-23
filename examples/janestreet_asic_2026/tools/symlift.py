#!/usr/bin/env python3
"""
symlift.py -- lift a flat sky130 gate netlist into symbolic next-state
equations.

The cell semantics are *not* re-implemented here: they are taken straight from
the validated :data:`sc_sim.CELLS` table by evaluating each cell's lambda with
:class:`Sym` operands, which overload ``&``, ``|``, ``^`` and ``1 - x``.  The
two cells whose lambda branches on a Python truth value (``mux2``/``mux2i``)
are special-cased.

API::

    lift = Lift('artifacts/puzzle_netlist.json')
    lift.flops            # {flop_inst: {'q': net, 'd': net, 'kind': 'dfrtp', ...}}
    lift.next_state[q]    # symbolic node for the D input of the flop driving q,
                          # in terms of var('<qnet>') and var('<primary input>')
    lift.out[net]         # symbolic node for any combinational net
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import symbolic as S
import sc_sim


class Sym:
    __slots__ = ('n',)

    def __init__(self, n):
        self.n = n

    def __and__(self, o):
        return Sym(S.AND(self.n, _n(o)))

    __rand__ = __and__

    def __or__(self, o):
        return Sym(S.OR(self.n, _n(o)))

    __ror__ = __or__

    def __xor__(self, o):
        return Sym(S.XOR(self.n, _n(o)))

    __rxor__ = __xor__

    def __rsub__(self, o):
        # only "1 - x" appears in the cell library
        assert o == 1, o
        return Sym(S.NOT(self.n))

    def __bool__(self):
        raise TypeError('symbolic value used in a boolean context')


def _n(o):
    if isinstance(o, Sym):
        return o.n
    if o in (0, 1):
        return S.ONE if o else S.ZERO
    raise TypeError(o)


POWER = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "KAPWR", "LOWHVPWR"}
OUTPUT_PINS = {"X", "Y", "Q", "Q_N", "COUT", "COUT_N", "SUM", "HI", "LO"}
SEQ_PREFIX = ('df', 'sdf', 'edf', 'dl')


class Lift:
    def __init__(self, path):
        d = json.load(open(path))
        self.raw = d
        self.ports = d['ports']
        self.insts = {i['name']: i for i in d['instances']
                      if not i.get('physical_only')}
        self.pi = sorted(p['net'] for p in self.ports.values()
                         if p['dir'] == 'input')
        self.po = sorted(p['net'] for p in self.ports.values()
                         if p['dir'] == 'output')

        self.driver = {}
        self.loads = {}
        for name, inst in self.insts.items():
            for pin, net in inst['connections'].items():
                if pin in POWER:
                    continue
                if pin in OUTPUT_PINS:
                    self.driver[net] = (name, pin)
                else:
                    self.loads.setdefault(net, []).append((name, pin))

        # sequential elements
        self.flops = {}
        for name, inst in self.insts.items():
            b = base(inst['cell'])
            cd = sc_sim.CELLS.get(b)
            if cd is not None and cd.seq is not None:
                c = inst['connections']
                self.flops[name] = {
                    'inst': name, 'kind': b,
                    'q': c.get('Q'), 'd': c.get('D'),
                    'clk': c.get('CLK'),
                    'reset_b': c.get('RESET_B'), 'set_b': c.get('SET_B'),
                    'x': inst['x'], 'y': inst['y'],
                }
        self.qnets = {f['q']: n for n, f in self.flops.items()}

        self.cuts = set()
        self._memo = {}
        self.next_state = {}
        for n, f in self.flops.items():
            self.next_state[f['q']] = self.net(f['d'])
        self.outputs = {p: self.net(p) for p in self.po
                        if p not in self.qnets}

    # ---- structured (cut) view -------------------------------------------
    def structured(self, max_fanout=1):
        """Equations that stop at every net whose fanout exceeds *max_fanout*.

        Returns ``(defs, order)`` where ``defs`` maps a net name to its
        symbolic expression over flop Q nets, primary inputs and other cut
        nets, and ``order`` lists the cut nets in topological order.
        """
        self.cuts = {n for n, l in self.loads.items()
                     if len(l) > max_fanout and n in self.driver
                     and n not in self.qnets}
        self._memo = {}
        defs = {}
        for name in sorted(self.cuts):
            defs[name] = self.net(name, _root=True)
        for n, f in self.flops.items():
            defs['D:' + f['q']] = self.net(f['d'], _root=True)
        for p in self.po:
            if p not in self.qnets:
                defs['O:' + p] = self.net(p, _root=True)
        # reset the flat memo so later flat queries are unaffected
        self._memo = {}
        return defs

    # ---- symbolic net evaluation ----
    def net(self, name, _stack=(), _root=False):
        if not _root and name in self.cuts:
            return S.var(name)
        if name in self._memo:
            return self._memo[name]
        if name in self.qnets or name in self.pi:
            r = S.var(name)
            self._memo[name] = r
            return r
        drv = self.driver.get(name)
        if drv is None:
            # genuinely undriven (n278) -- treated as 0, matching sc_sim
            r = S.ZERO
            self._memo[name] = r
            return r
        inst, opin = drv
        if name in _stack:
            raise RuntimeError('combinational loop at %s' % name)
        _root_cut = _root and name in self.cuts
        cell = self.insts[inst]['cell']
        b = base(cell)
        cd = sc_sim.CELLS[b]
        conns = self.insts[inst]['connections']
        stack = _stack + (name,)
        args = {}
        for pin in cd.inputs:
            net = conns.get(pin)
            args[pin] = Sym(self.net(net, stack)) if net is not None else Sym(S.ZERO)
        if b in ('mux2', 'mux2i'):
            r = S.MUX(args['S'].n, args['A0'].n, args['A1'].n)
            if b == 'mux2i':
                r = S.NOT(r)
        else:
            fn = cd.outputs[opin]
            r = _n(fn(args))
        if not _root_cut:
            self._memo[name] = r
        return r


def base(cell):
    c = cell.replace('sky130_fd_sc_hd__', '')
    return re.sub(r'_\d+$', '', c)
