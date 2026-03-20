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

# Patch shutil.move to handle concurrent RCC runs sharing the same holotree.
# t_vault's install_bitwarden() calls shutil.move(bw_binary, scripts_path) and
# crashes with "already exists" when two processes race. This wraps shutil.move
# to catch that specific error — must happen BEFORE any t_vault import since
# t_vault.__init__ triggers Bitwarden() → install_bitwarden() at import time.
import shutil as _shutil
import os as _os
_orig_shutil_move = _shutil.move

def _shutil_move_no_race(src, dst, copy_function=_shutil.copy2):
    try:
        return _orig_shutil_move(src, dst, copy_function)
    except _shutil.Error as e:
        if "already exists" in str(e):
            if _os.path.isfile(src):
                _os.remove(src)
            real_dst = _os.path.join(dst, _os.path.basename(src)) if _os.path.isdir(dst) else dst
            return real_dst
        raise

_shutil.move = _shutil_move_no_race

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
