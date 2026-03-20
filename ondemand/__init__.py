"""
Ondemand — Automation agent toolkit for the Ondemand platform.

Provides supervised execution, step tracking, dynamic manifests,
artifact management, HITL approvals, and R2 storage integration.

    from ondemand import supervised_step, request_approval, Status

    @supervised_step("Process Data")
    def process(self):
        # your automation logic
        pass
"""

# Patch t_vault's Bitwarden CLI installer to handle concurrent RCC runs.
# Two runs sharing the same holotree race on shutil.move → "already exists".
# Must run before any robot code does `from t_vault import ...`.
try:
    import t_vault.utils.core.download_bitwarden as _bw_mod
    import shutil as _shutil
    import os as _os
    _orig_bw_install = _bw_mod.install_bitwarden

    def _safe_install_bitwarden(force_latest=False):
        _real_move = _shutil.move
        def _move_no_race(src, dst, *a, **kw):
            dest = _os.path.join(dst, _os.path.basename(src)) if _os.path.isdir(dst) else dst
            if _os.path.exists(dest):
                _os.remove(src)
                return dest
            return _real_move(src, dst, *a, **kw)
        _shutil.move = _move_no_race
        try:
            return _orig_bw_install(force_latest)
        finally:
            _shutil.move = _real_move

    _bw_mod.install_bitwarden = _safe_install_bitwarden
except ImportError:
    pass  # t_vault not installed — no patch needed

from .supervisor import (
    supervised,
    supervised_step,
    connect_to_ondemand,
    send_manifest,
    build_manifest_step,
    update_manifest,
    ManifestStep,
    step_scope,
    Status,
)

from .shared.approval import request_approval, ApprovalRequestError

__all__ = [
    "supervised",
    "supervised_step",
    "connect_to_ondemand",
    "send_manifest",
    "build_manifest_step",
    "update_manifest",
    "ManifestStep",
    "step_scope",
    "Status",
    "request_approval",
    "ApprovalRequestError",
]
