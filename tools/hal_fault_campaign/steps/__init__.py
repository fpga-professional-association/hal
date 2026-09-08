"""The half of hal_fault_campaign that runs inside HAL.

Everything in this package may import ``hal_py``; nothing outside it may.  It is
executed by ``hal --python-script`` through :mod:`campaign_runner`, which is a
plain file rather than a module because that is what ``--python-script`` takes.
"""
