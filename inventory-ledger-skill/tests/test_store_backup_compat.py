"""在受限 Python 执行环境中，已存在的备份目录也应允许连续保存。"""
from __future__ import annotations

import errno
from pathlib import Path

from ledgerlib import schema as S
from ledgerlib.store import Store


def test_repeated_save_when_pathlib_mkdir_spuriously_raises_eexist(ledger, monkeypatch):
    """模拟 WorkBuddy 对 Path.mkdir(exist_ok=True) 的 EEXIST 异常行为。"""
    backups = Path(ledger).parent / "backups"

    with Store.open(ledger, for_write=True) as st:
        st.save()  # 第二次写盘：首次建立 backups/
        assert backups.is_dir()

        original_mkdir = Path.mkdir

        def restricted_mkdir(path, *args, **kwargs):
            if path == backups and path.is_dir():
                raise FileExistsError(errno.EEXIST, "sandbox directory already exists", str(path))
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", restricted_mkdir)
        st.save()  # 第三次写盘：已有备份目录，不得因 EEXIST 失败

    with Store.open(ledger, for_write=False) as st:
        assert st.meta(S.META_WRITES) == "3"
    assert len(list(backups.glob("*.xlsx"))) == 2
