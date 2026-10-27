#!/usr/bin/env python3
"""进销存台账 V1 命令行入口。

用法概览(完整说明见 references/workflows.md 与 SKILL.md):
  cli.py init --path 台账.xlsx --company 名称 --start-period 2026-01
  cli.py sku add --sku S --name 商品 --unit 件
  cli.py wh add --wh A --name A仓
  cli.py import --file x.csv --type purchase|sale|transfer|adjust|fee|opening [--preview|--commit]
  cli.py fee allocate --fee-id FEE-000001 [--explicit IN-1:10.00,...] [--preview]
  cli.py cost confirm --ids IN-000001,... --basis "..."
  cli.py cost correct --id IN-000001 --new-price 12.5 --basis "..."
  cli.py close dry|final --period 2026-01
  cli.py reopen --period 2026-01 --auth 授权 --reason 原因
  cli.py query stock|flows|monthly|exceptions|pending|status [...]
所有写操作要求 --confirm 用户确认(对应任务书"获得用户确认"步骤)。
带 --json 输出机器可读结果, 供智能体消费。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from ledgerlib.store import Store, LedgerError
from ledgerlib import biz, engine, importer, queries


def _err(e: LedgerError) -> int:
    print(json.dumps({"ok": False, "code": e.code, "message": e.message},
                     ensure_ascii=False))
    return 1


def _ok(data) -> int:
    print(json.dumps({"ok": True, **data}, ensure_ascii=False))
    return 0


def _open(path: str, write: bool) -> Store:
    return Store.open(path, for_write=write)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cli.py", description="进销存台账 V1")
    p.add_argument("--json", action="store_true", help="JSON 输出(默认)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", default="进销存台账.xlsx", help="台账文件路径")
    common.add_argument("--operator", default="cli", help="操作人标识")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_):
        sp = sub.add_parser(name, help=help_, parents=[common])
        return sp

    # init
    sp = add("init", "初始化台账")
    sp.add_argument("--company", required=True)
    sp.add_argument("--start-period", required=True)

    # sku / wh
    sp = add("sku", "商品档案")
    ssub = sp.add_subparsers(dest="sub", required=True)
    sp2 = ssub.add_parser("add", parents=[common])
    sp2.add_argument("--sku", required=True); sp2.add_argument("--name", required=True)
    sp2.add_argument("--unit", required=True); sp2.add_argument("--remark", default="")
    sp2 = ssub.add_parser("list", parents=[common])

    sp = add("wh", "仓库档案")
    ssub = sp.add_subparsers(dest="sub", required=True)
    sp2 = ssub.add_parser("add", parents=[common])
    sp2.add_argument("--wh", required=True); sp2.add_argument("--name", required=True)
    sp2.add_argument("--remark", default="")
    sp2 = ssub.add_parser("list", parents=[common])

    # import
    sp = add("import", "文件导入(先预览后提交)")
    sp.add_argument("--file", required=True)
    sp.add_argument("--type", choices=["purchase", "sale", "transfer", "adjust", "fee", "opening"],
                    required=True)
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--commit", action="store_true")
    sp.add_argument("--confirm", default="", help="用户确认(commit 必需)")
    sp.add_argument("--allow-partial", action="store_true")

    # fee allocate
    sp = add("fee", "附加费用")
    ssub = sp.add_subparsers(dest="sub", required=True)
    sp2 = ssub.add_parser("allocate", parents=[common])
    sp2.add_argument("--fee-id", required=True)
    sp2.add_argument("--explicit", default="", help="IN-000001:12.50,IN-000002:87.50")
    sp2.add_argument("--preview", action="store_true")
    sp2.add_argument("--confirm", default="")

    # cost
    sp = add("cost", "采购成本")
    ssub = sp.add_subparsers(dest="sub", required=True)
    sp2 = ssub.add_parser("confirm", parents=[common])
    sp2.add_argument("--ids", required=True)
    sp2.add_argument("--basis", required=True)
    sp2.add_argument("--confirm", default="")
    sp2 = ssub.add_parser("correct", parents=[common])
    sp2.add_argument("--id", required=True)
    sp2.add_argument("--new-price", default="")
    sp2.add_argument("--new-amount", default="")
    sp2.add_argument("--basis", required=True)
    sp2.add_argument("--confirm", default="")

    # close / reopen
    sp = add("close", "期间结账")
    ssub = sp.add_subparsers(dest="sub", required=True)
    sp2 = ssub.add_parser("dry", parents=[common])
    sp2.add_argument("--period", required=True)
    sp2 = ssub.add_parser("final", parents=[common])
    sp2.add_argument("--period", required=True)
    sp2.add_argument("--confirm", required=True, help="用户明确确认")

    sp = add("reopen", "受控重开")
    sp.add_argument("--period", required=True)
    sp.add_argument("--auth", required=True, help="授权标识")
    sp.add_argument("--reason", required=True)

    # query
    sp = add("query", "查询")
    ssub = sp.add_subparsers(dest="sub", required=True)
    sp2 = ssub.add_parser("stock", parents=[common])
    sp2.add_argument("--period", default=""); sp2.add_argument("--sku", default="")
    sp2.add_argument("--wh", default="")
    sp2 = ssub.add_parser("flows", parents=[common])
    sp2.add_argument("--period", default=""); sp2.add_argument("--kind", default="")
    sp2.add_argument("--sku", default="")
    sp2 = ssub.add_parser("monthly", parents=[common])
    sp2.add_argument("--period", required=True)
    sp2 = ssub.add_parser("exceptions", parents=[common])
    sp2.add_argument("--period", required=True)
    sp2 = ssub.add_parser("pending", parents=[common])
    sp2.add_argument("--period", default="")
    sp2 = ssub.add_parser("status", parents=[common])

    args = p.parse_args(argv)
    path, operator = args.path, args.operator
    try:
        return _dispatch(args, path, operator)
    except LedgerError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001 未预期错误也给出结构化输出
        return _err(LedgerError("E10", f"未预期错误: {e!r}"))


def _dispatch(args, path: str, operator: str) -> int:
    a = args
    if a.cmd == "init":
        Store.create(path, a.company, a.start_period, operator)
        return _ok({"path": path, "company": a.company, "start_period": a.start_period})
    if a.cmd == "sku":
        st = _open(path, write=(a.sub == "add"))
        if a.sub == "add":
            biz.add_sku(st, a.sku, a.name, a.unit, operator, a.remark)
            st.close()
            return _ok({"sku": a.sku})
        data = [{"SKU": r.get("SKU"), "名称": r.get("商品名称"),
                 "单位": r.get("基本单位"), "状态": r.get("状态")}
                for r in st.read_all(__import__("ledgerlib.schema", fromlist=["SHEET_SKU"]).SHEET_SKU)]
        st.close()
        return _ok({"sku": data})
    if a.cmd == "wh":
        st = _open(path, write=(a.sub == "add"))
        if a.sub == "add":
            biz.add_warehouse(st, a.wh, a.name, operator, a.remark)
            st.close()
            return _ok({"wh": a.wh})
        data = [{"仓库代码": r.get("仓库代码"), "名称": r.get("仓库名称"),
                 "状态": r.get("状态")} for r in st.read_all("仓库档案")]
        st.close()
        return _ok({"warehouse": data})
    if a.cmd == "import":
        if a.commit and not a.confirm:
            return _err(LedgerError("E08", "提交必须提供 --confirm 用户确认"))
        st = _open(path, write=a.commit)
        if a.preview:
            pr = importer.preview(st, a.file, a.type)
            st.close()
            return _ok(pr.summary)
        res = importer.commit(st, a.file, a.type, operator, a.confirm, a.allow_partial)
        st.close()
        return _ok(res)
    if a.cmd == "fee":
        st = _open(path, write=not a.preview)
        if a.explicit:
            explicit = []
            for pair in a.explicit.split(","):
                rid, amt = pair.split(":")
                explicit.append((rid.strip(), Decimal(amt.strip())))
        else:
            explicit = None
        if a.preview:
            plan = biz.plan_fee_allocation(st, a.fee_id, explicit)
            st.close()
            return _ok(plan)
        if not a.confirm:
            st.close()
            return _err(LedgerError("E08", "分摊执行必须提供 --confirm 用户确认"))
        res = biz.allocate_fee(st, a.fee_id, operator, a.confirm, explicit)
        st.close()
        return _ok(res)
    if a.cmd == "cost":
        st = _open(path, write=True)
        if a.sub == "confirm":
            if not a.confirm:
                st.close()
                return _err(LedgerError("E08", "成本确认必须提供 --confirm 用户确认"))
            ids = [x.strip() for x in a.ids.split(",") if x.strip()]
            res = biz.confirm_cost(st, ids, operator, a.confirm, a.basis)
            st.close()
            return _ok(res)
        if not a.confirm:
            st.close()
            return _err(LedgerError("E08", "成本更正必须提供 --confirm 用户确认"))
        res = biz.correct_purchase(st, a.id, operator, a.confirm, a.basis,
                                   a.new_price or None, a.new_amount or None)
        st.close()
        return _ok(res)
    if a.cmd == "close":
        st = _open(path, write=(a.sub == "final"))
        if a.sub == "dry":
            report = engine.dry_close(st, a.period)
            data = {"期间": a.period, "ok": report.ok,
                    "异常": [{"code": c, "message": m} for c, m in report.exceptions]}
            st.close()
            return _ok(data)
        report = engine.final_close(st, a.period, operator, a.confirm)
        st.close()
        return _ok({"期间": a.period, "ok": True,
                    "异常": [{"code": c, "message": m} for c, m in report.exceptions]})
    if a.cmd == "reopen":
        st = _open(path, write=True)
        engine.reopen(st, a.period, a.auth, a.reason, operator)
        st.close()
        return _ok({"期间": a.period, "reopened": True})
    if a.cmd == "query":
        st = _open(path, write=False)
        if a.sub == "stock":
            res = queries.query_stock(st, a.period or None, a.sku or None, a.wh or None)
        elif a.sub == "flows":
            res = queries.query_flows(st, a.period or None, a.kind or None, a.sku or None)
        elif a.sub == "monthly":
            res = queries.query_monthly(st, a.period)
        elif a.sub == "exceptions":
            res = queries.query_exceptions(st, a.period)
        elif a.sub == "pending":
            res = queries.query_pending(st, a.period or None)
        else:
            res = queries.status(st)
        st.close()
        return _ok(res)
    return _err(LedgerError("E08", f"未知命令: {a.cmd}"))


if __name__ == "__main__":
    sys.exit(main())