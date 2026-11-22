"""Astrophoto. The whole application runs offline: every catalogue and table is local (built into the image),
and astropy is told never to reach the internet (bundled IERS / constellation data only)."""
try:
    from astropy.utils import iers as _iers
    from astropy.utils.data import conf as _data_conf
    _data_conf.allow_internet = False
    _iers.conf.auto_download = False
    _iers.conf.auto_max_age = None
    _iers.conf.iers_degraded_accuracy = "warn"
except Exception:                                  # astropy missing / older API: nothing to switch off
    pass
