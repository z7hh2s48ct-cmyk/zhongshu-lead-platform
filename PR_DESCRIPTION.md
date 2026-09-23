# 9.22 / 9.23 客资反馈：9 条问题修复与功能补齐

基于 0d1c97f（2026-09-21 基线）。覆盖 9.22（6 条）与 9.23（3 条）全部反馈，口径依据 `docs/requirements/2026-09-23_922-923反馈需求口径定稿.md`（客户确认 + 未答复项按建议默认拍板，默认项的回调影响评估见定稿文档第四节）。

## 分批提交（7 个 commit）

| commit | 内容 | 对应反馈 |
|---|---|---|
| f2f06de | 需求口径定稿文档 | 全部 |
| a154ef5 | 待终审改蓝；H5 城市框走后端搜县级回填市县；电销指派信息并入状态标签+改派按钮 | 9.23-2、9.22-2、9.22-4 |
| 9eae164 | 保留授权功能，放开运营端代改（任何状态、不强制原因、自动审计） | 9.22-1 |
| d53f98e | 撤销缺县硬门槛（3 处拦截+5 处改写），状态机补边，软提醒+缺县派发标记，存量解卡脚本 | 9.22-5、9.22-6 |
| fb794ff | 来源细化 (a)(b)(c)：渠道筛选、直播/广告枚举、关键词搜索覆盖具体来源；运营可编辑来源选项（SystemConfig） | 9.23-1 |
| 8c68701 | 提现入口（H5 申请+后台审核核销）、额度/费率超管可配（默认 100 积分/0%）、管理权限收紧为仅超管 | 9.22-3 |
| f91c45f | 电销页筛选（8 维度：姓名/手机号/状态/人员/结论/参考时间区间/来源），手机号权限显隐，补索引 | 9.23-3 |
| （本次） | 全量回归修复：按新口径同步既有契约测试 | — |

## 主要设计决策（默认拍板项）

- **缺县直派**：(b)+(c) 组合——放开直派 + 派发弹窗软提醒 + `district_missing` 标记留痕（lead_snapshot 与派发审计）。
- **授权代改**：consent_confirmed 移出更正只读集；仅改授权不强制原因；False→True 解锁畅通，已审核客资改回 False 仍被提交校验拒绝（422 LEAD_SUBMISSION_INVALID）。
- **自定义来源**：SystemConfig 整组存储（版本化+审计），未配置回落内置 7 项默认；新权限 `source.channel.manage` 授予 OPERATION；历史数据不回刷，靠关键词搜索覆盖 source_detail。
- **提现**：策略走 SystemConfig（默认最低 100 积分、费率 0%），手续费审核时快照，付款/核销按「应付=现金-手续费」校验；五个管理端点由 `reward.read` 收紧为 `*`。
- **电销筛选**：手机号精确搜索走 phone_hash（不支持模糊，前端按 `lead.phone.export` 显隐）；电销角色本人强制限定不被筛选覆盖。

## 存量数据

新增 `scripts/unblock_district_pending_leads.py`（幂等 + dry-run 默认 + 审计），把被旧规则卡死（`DISTRICT_PENDING_VERIFY`）的存量客资按现行路由恢复入池。上线后执行：先 dry-run 预览，确认后加 `--apply`。删除了前提失效的 `backfill_county_missing_pool_leads.py`。

## 数据库迁移

- `0025_withdrawal_fee_snapshots`：提现表加两个可空快照列（可回滚）
- `0026_telesales_filter_indexes`：verification_conclusion、customer_name 索引（可回滚）

## 测试

- 新增 5 个验收测试文件（约 35 用例），覆盖全部 9 条反馈的服务端行为与前端契约。
- 全量 `pytest apps/api/tests` 通过（含 worktree 清洁守护）。
- 按新口径同步更新既有测试：919 batch2 S7（缺县）、919 batch5 S8（授权）、829 item4/item5、语言契约、图标契约、版本号钉住断言。

## 回滚

代码按 commit 粒度 `git revert` 可干净回滚；两个迁移均有 downgrade。注意事项：`source.channel.manage` 权限与 `DISTRICT_PENDING_VERIFY` 存量脚本不影响回滚（脚本独立、权限回收即可）。
