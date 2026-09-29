# 2026-09-29 客资反馈接口与状态口径

本文件记录本次实现新增的接口和与旧版不同的状态判定。生产环境仍需按顺序执行数据库迁移 `0029_lead_customer_wechat`、`0030_owner_wechat_rebind_invite`，再启用新版服务。存在客户微信号数据时，`0029` 会拒绝降级以防联系方式丢失；回滚需先将此类记录安全迁移到可保留的结构。

## 客资联系方式与流转

- 录入草稿支持 `customer_wechat`。正式提交至少有有效手机号或客户微信号；若填写手机号，手机号仍须有效。客户微信号按大小写不敏感的规范值查重，原文加密存储。
- 仅知省份的记录保存为待补草稿，补全城市等派发资料后才能正式提交。
- 平台和加盟商供资在当地无合格接收方时进入 `PUBLIC_POOL`。公海池转派发池时重新校验资料、查重与接收覆盖。
- 平台快捷派发先用 `POST /api/v1/v1.2/platform/leads/quick-dispatch/candidates` 预览；无当地接收方时返回 `pool_target=PUBLIC_POOL`。随后 `POST /api/v1/v1.2/platform/leads/quick-dispatch/public-pool` 原子创建并正式提交，最终状态为 `PUBLIC_POOL`、原因为 `PUBLIC_POOL_NO_LOCAL_RECEIVER`；接收条件变化时返回 409，需重新选择。
- 已领取客资的详情可显示客户微信号；未领取时沿用既有联系方式可见范围。
- 电销工作台通过 `POST /api/v1/v1.2/pre-dispatch-verifications/customer-wechat-exists` 以 `{"customer_wechat":"..."}` 精确查重，仅向有前置核验任务权限的电销返回全平台未删除客资是否存在，不返回客户详情，并记录查询审计。仅有微信号的本人任务开始核验后展示完整微信号，不显示拨号按钮。

## 加盟商批量导入

- `GET /api/v1/v1.2/supplier/leads/import-template` 下载 Excel 模板。
- `POST /api/v1/v1.2/supplier/leads/import` 上传 `.xlsx` 文件，逐行校验并返回成功、待补草稿与失败行结果。仅知省份的行保存为 `DRAFT`，补全城市后提交；单文件上限 2 MB、解压后 8 MB、200 条客资。

## 负责人更换微信

- `POST /api/v1/auth/companies/{company_id}/owner-wechat-rebind-invites` 需要 `company.account.manage` 权限，请求体 `{"expires_hours": 72}`。响应一次性返回换绑链接和邀请文案。
- 新微信通过既有 `/auth/invites/preview`、`/auth/invites/confirm-start` 和微信 OAuth 回调消费邀请。成功时更新原 `User` 的 `WechatIdentity` 并递增 `session_version`；负责人用户 ID、公司、权限和业务历史不变。旧微信在成功前仍可登录，成功后旧会话失效。
- 邀请一次性使用；新微信已绑定其他用户、公司或负责人已变更时拒绝换绑。再次发起邀请会撤销该公司的未使用邀请。

## 退回终审与改派

- `POST /api/v1/v1.2/returns/{return_id}/correct-and-redispatch` 在一次事务内完成运营终审同意退回、原领取流水返分、客户信息更正和派发给另一加盟商。
- 请求包含 `company_id`、可选 `employee_user_id`、`idempotency_key`、客户更正字段、`reason` 和可选 `expected_snapshot_version`。同一幂等键重试必须使用相同请求内容；页面会传当前快照版本并在网络重试时沿用同一键。
- 运营终审可参考电销结论独立决定是否退回。仅更正区域并重新入池的既有入口保留。
