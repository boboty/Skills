#!/usr/bin/env bash
# 模拟日常业务端到端演示: 建台账 -> 期初 -> 采购/销售/调拨/调整/费用 -> 结账 -> 下月业务 -> 结账
# 输出: output/进销存台账.xlsx + output/transcript.log (全部命令与程序输出)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CLI="python3 $ROOT/scripts/cli.py"
LEDGER="output/进销存台账.xlsx"
LOG="output/transcript.log"
mkdir -p output
rm -f "$LEDGER" && rm -rf output/backups

log() { echo "### $*" | tee -a "$LOG"; }
run() { echo "\$ $*" | tee -a "$LOG"; eval "$*" 2>&1 | tee -a "$LOG"; echo; }

log "===== 进销存台账 V1 模拟日常业务(演示/验收证据) ====="
log "时间: $(date '+%Y-%m-%d %H:%M:%S')"

log "--- 1. 初始化台账 ---"
run "$CLI init --path $LEDGER --company 演示商贸有限公司 --start-period 2026-01 --operator demo"

log "--- 2. 商品/仓库建档 ---"
run "$CLI sku add --path $LEDGER --sku SKU-001 --name 演示商品甲 --unit 件 --operator demo"
run "$CLI sku add --path $LEDGER --sku SKU-002 --name 演示商品乙 --unit 个 --operator demo"
run "$CLI wh add --path $LEDGER --wh W01 --name 主仓 --operator demo"
run "$CLI wh add --path $LEDGER --wh W02 --name 分仓 --operator demo"

log "--- 3. 期初库存: 预览 -> 确认提交 ---"
run "$CLI import --path $LEDGER --file data/01_期初.csv --type opening --preview"
run "$CLI import --path $LEDGER --file data/01_期初.csv --type opening --commit --confirm 用户确认期初 --operator demo"

log "--- 4. 采购入库(两单, 含批次/多行) ---"
run "$CLI import --path $LEDGER --file data/02_采购_01.csv --type purchase --commit --confirm 用户确认采购 --operator demo"
run "$CLI import --path $LEDGER --file data/03_采购_02.csv --type purchase --commit --confirm 用户确认采购 --operator demo"

log "--- 5. 销售出库(两单, 跨仓) ---"
run "$CLI import --path $LEDGER --file data/04_销售_01.csv --type sale --commit --confirm 用户确认销售 --operator demo"
run "$CLI import --path $LEDGER --file data/05_销售_02.csv --type sale --commit --confirm 用户确认销售 --operator demo"

log "--- 6. 跨仓调拨 W01 -> W02 20 件 ---"
run "$CLI import --path $LEDGER --file data/06_调拨.csv --type transfer --commit --confirm 用户确认调拨 --operator demo"

log "--- 7. 库存调整: 报废 SKU-002 5 个 ---"
run "$CLI import --path $LEDGER --file data/07_调整.csv --type adjust --commit --confirm 用户确认调整 --operator demo"

log "--- 8. 附加费用 150 元(按数量比例分摊到 PO-001 两行) ---"
run "$CLI import --path $LEDGER --file data/08_费用.csv --type fee --commit --confirm 用户确认费用 --operator demo"
run "$CLI fee allocate --path $LEDGER --fee-id FEE-000001 --preview"
run "$CLI fee allocate --path $LEDGER --fee-id FEE-000001 --confirm 用户确认分摊 --operator demo"

log "--- 9. 月末试结账(只计算, 不落盘) ---"
run "$CLI close dry --path $LEDGER --period 2026-01"
run "$CLI query monthly --path $LEDGER --period 2026-01"
run "$CLI query stock --path $LEDGER"

log "--- 10. 正式结账 2026-01(用户确认) ---"
run "$CLI close final --path $LEDGER --period 2026-01 --confirm 用户确认结账 --operator demo"
run "$CLI query status --path $LEDGER"

log "--- 11. 开启 2026-02 并继续日常 ---"
run "$CLI period open --path $LEDGER --period 2026-02 --operator demo"
run "$CLI import --path $LEDGER --file data/09_下月采购.csv --type purchase --commit --confirm 用户确认采购 --operator demo"
run "$CLI import --path $LEDGER --file data/10_下月销售.csv --type sale --commit --confirm 用户确认销售 --operator demo"
run "$CLI close final --path $LEDGER --period 2026-02 --confirm 用户确认结账 --operator demo"

log "--- 12. 两月结存核对(1月末=2月初 自动衔接) ---"
run "$CLI query monthly --path $LEDGER --period 2026-01"
run "$CLI query monthly --path $LEDGER --period 2026-02"
run "$CLI query stock --path $LEDGER"

log "===== 演示结束: 台账与日志输出于 output/ ====="
echo "生成文件:" | tee -a "$LOG"
ls -la output/ | tee -a "$LOG"