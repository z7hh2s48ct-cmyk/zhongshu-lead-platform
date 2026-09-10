# 合家美宅客资平台 V1.2.5

代码仓库：<https://github.com/peihr666-max/zhongshu-lead-platform>。
2026-09-10 起发布使用该仓库的 `main` 提交及其 GitHub Actions 证据；部署来源校验的 `--expected-repository` 必须传入 `peihr666-max/zhongshu-lead-platform`。旧仓库的构建证据不得用于证明新仓库的发布，新增性能审批也必须引用新仓库的数字 Issue/PR 链接。历史发布记录保留原始地址以便追溯。

来源校验默认只接受成功的 `main` push 构建。自动触发不可用时，可显式传入 `--allow-owner-dispatch`：仅接受个人仓库所有者本人首次触发的 `main` 手动构建，原始触发人和本次触发人的账号 ID 均必须与仓库所有者一致，源码仓库 ID 必须一致。选定 SHA 必须与发布时实际远端 `main` 一致；成功状态、可信工作流、未过期同次构建产物以及镜像与扫描一致性校验均保留。报告如实记录 `workflow_dispatch`，不支持任意协作者或重跑产物冒充推送构建。

合家美宅 V1.2.5 是客资供给、审核、人工派发、加盟商领取、退回申诉、供应奖励、通知、报表与审计的一体化生产候选版本。

> 交付状态分为 `代码完成`、`自动化通过`、`真实环境验收`。前两项不能替代真实微信、目标基础设施、生产数据、业务 UAT、灾备和灰度验收。

## 已冻结的业务边界

- 平台手工或飞书导入客资可在手机号有效且无未决重复时由运营直接派发前置电销核验，电销提交后回到运营处置；
- 加盟商客资需具备有效供给能力并通过运营资料初审；资料不全时同样由运营派发前置电销核验；
- 电销只能处理运营指派的 `PRE_DISPATCH_VERIFY` 或 `RETURN_VERIFY`，提交事实结论后由运营决定后续处置；
- 退回证据为聊天截图或电话录音至少一项；
- 退回申诉期从成功领取起连续计算 48 小时；供应奖励观察期仍独立按 3 个工作日计算；
- V1.2 仅支持平台人工派发，不提供自动、随机、轮询、权重或抢单；
- 禁止供应商将自己提供的客资派发给自己；
- 奖励仅在观察期结束且不存在有效申诉时结算，异常冲正单独审计。

V1.2 不包含在线支付、自动派发、微信小程序、云外呼或 H5 自动录音。

## 技术实现

- FastAPI、SQLAlchemy 2、Alembic；
- PostgreSQL 16 生产主库，SQLite 仅用于本地开发和部分单元测试；
- 加盟商微信 H5、内部电销 H5、平台管理后台；
- API、Scheduler 与完整手机号导出 Worker 分进程运行，大导出不阻塞超时、通知和积分定时任务；
- 腾讯云 COS 上海地域私有 Bucket 保存截图和录音；
- Nginx、HTTPS、RBAC、字段隔离、结构化日志、备份恢复和审计追踪。

## 本地开发

```bash
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py
python scripts/seed_demo.py
uvicorn apps.api.src.main:app --host 0.0.0.0 --port 8000 --reload
```

访问入口：

- 加盟商 H5：`http://localhost:8000/h5/`
- V1.2 加盟商工作台：`http://localhost:8000/h5/v12-workbench.html`
- 平台管理工作台：`http://localhost:8000/admin/`
- 平台响应式工作台：`http://localhost:8000/h5/admin/`
- 内部电销 H5：`http://localhost:8000/h5/call/`
- 加盟商 H5：`http://localhost:8000/h5/`
- OpenAPI：`http://localhost:8000/docs`

演示账号只允许用于开发和自动验收，见 `docs/runbooks/DEMO_ACCOUNTS.md`。

## 全国地区组件

管理后台新建加盟商时通过 `GET /api/v1/master-data/region-tree` 获取省、市、区县三级下拉数据，用户无需填写地区编码。接口内置 2025 年中国大陆地区快照；只有实际选中的城市和区县会写入现有地区表并参与派发、积分与服务范围联动。

快照来自开源项目 [xihan123/gb2260](https://github.com/xihan123/gb2260)，使用 CC0-1.0 许可，并非官方实时接口。更新快照时必须同步执行地区树、加盟商创建和派发范围回归测试。

## 加盟商停用与测试数据清理

超级管理员和运营人员都可以在管理后台的“加盟商”列表停用或重新启用主体。停用会立即使该加盟商人员的已有会话失效，阻止继续登录和处理新业务，但不会删除历史客资、派发或积分记录。

已停用且明确标记为测试的主体可以执行完整清理，不因已产生业务或已派发客资而阻止。清理范围包括加盟商主体、积分账户、充值/调账/冲正流水、自有客资及其已派发业务链、作为接收方产生的派发、退回、跟进、奖励、邀请和业务消息。测试主体只是客资接收方时，系统保留其他主体的原始客资，仅清理测试派发及后续记录。退回证据的数据库记录与可重试的对象存储清理任务在同一事务内落库，调度器后续幂等删除文件。审计记录始终保留，原成员账号会立即停用并解除公司/微信关联。

永久删除只允许超级管理员执行，必须在弹窗中准确输入加盟商完整名称、填写操作原因并二次确认。历史上未标记的测试主体必须先读取清理影响预览，再输入固定短语“永久删除测试数据”和当次预览令牌完成标记；范围变化后旧令牌立即失效。正常加盟商不能直接删除。

| 方法 | 路径 | 用途 |
|---|---|---|
| `PATCH` | `/api/v1/companies/{company_id}` | 通过 `status` 和 `reason` 停用或重新启用加盟商主体 |
| `GET` | `/api/v1/companies/{company_id}/purge-preview` | 超级管理员预览历史主体清理范围并取得一次性范围令牌 |
| `POST` | `/api/v1/companies/{company_id}/mark-test` | 超级管理员凭完整名称、固定确认短语和最新范围令牌标记历史测试主体 |
| `DELETE` | `/api/v1/companies/{company_id}` | 超级管理员完整清理已停用测试主体，Body 必须包含 `confirm_name` 和 `reason` |

## 内部账号停用与测试数据清理

内部账号停用后会立即失去登录和业务处理能力，但账号与历史数据保留。仅已停用、已标记为测试、且全库没有客资、派发、核验、退回、积分或其他业务引用的内部账号可以删除。标记与删除都必须填写完整登录账号、操作原因并二次确认；审计记录始终保留。

## 自动质量门禁

面向 `release/v1.2.5` 的 PR 和发布分支执行：

1. 全量 Python 测试、JavaScript 检查、密钥扫描、编译和空白检查；
2. SQLite V1.0.1 → V1.2 升降级循环；
3. PostgreSQL 16 历史夹具升级、手机号指纹回填、数据对账和再升级；
4. Chromium 桌面管理后台与移动 H5 的真实浏览器交互和截图检查。

## T30 历史数据迁移

生产环境必须在维护窗口、写入冻结和备份完成后，通过生产 Compose 网络执行迁移和对账。正式命令见 `docs/runbooks/V1.2_MIGRATION_RUNBOOK.md` 与 `docs/runbooks/DEPLOYMENT.md`。

迁移任务使用 `v12_migration_checkpoints` 断点续跑；检查点和错误样本只保存业务 ID 与错误码，不保存明文手机号。最终对账证据固定持久化到宿主机 `dist/v12-reconciliation.json`，不得仅写入 `docker compose run --rm` 的临时容器。

## 生产部署

```bash
cp .env.docker.example .env
# 填写正式域名、独立随机密钥、微信、PostgreSQL、对象存储和不可变 APP_IMAGE
python scripts/validate_production_env.py --env-file .env
python scripts/verify_production.py \
  --env-file .env \
  --require-certificates \
  --require-image-digest \
  --require-image-inspect \
  --scan-subject scan-subject.json
docker compose --env-file .env -f docker-compose.yml -f docker-compose.prod.yml up -d db
python scripts/preflight_v12.py \
  --env-file .env \
  --require-certificates \
  --compose-database \
  --scan-subject scan-subject.json \
  --output dist/v12-preflight.json
```

当 `.env` 只配置 `POSTGRES_*` 时，配置校验会推导与 Compose 一致的内部 PostgreSQL URL；真正的 Alembic revision 和 V1.2 数据对账由 `--compose-database` 通过 API 一次性容器在生产 Compose 网络执行，避免宿主机误连 SQLite。

飞书是显式可选集成：启用时设置 `FEISHU_ENABLED=true`，配置应用、数据表和“客户视图”，并保持 `FEISHU_WRITEBACK_ENABLED=false`。一期仅允许运营在公海池手动触发单向导入，不执行定时同步或飞书回写；不启用时不得保留无效生产凭据。公海池按系统派生的“运营录入/加盟商提供”区分客户来源，加盟商供客在当地没有其他合格接收方时暂存，待运营手动重新匹配。

完整手机号报表仅允许有导出权限的后台运营人员创建。`lead-export-worker` 每次只处理 1 个任务，生成可直接下载的 XLSX 工作簿，并记录导出人、筛选条件、文件摘要和到期清理任务；上传前先持久化清理意图，进程中断后也能删除孤儿敏感文件。

生产操作文档：

- `docs/runbooks/DEPLOYMENT.md`
- `docs/runbooks/PRODUCTION_CHECKLIST_V1.2.md`
- `docs/runbooks/V1.2_INITIALIZATION_SOP.md`
- `docs/runbooks/V1.2_MIGRATION_RUNBOOK.md`
- `docs/runbooks/V1.2_UAT.md`
- `docs/runbooks/V1.2_GO_NO_GO.md`
- `docs/runbooks/V1.2_ROLLBACK.md`
- `docs/runbooks/V1.2_POST_LAUNCH.md`
- `docs/runbooks/BACKUP_RESTORE.md`
- `docs/runbooks/WECHAT_GATE0.md`

真实服务号联调、真实加盟商 UAT、生产备份恢复演练和灰度开放必须在目标环境执行，不能由代码测试替代。

## 发布打包

```bash
python scripts/check_release_metadata.py
python scripts/package_release.py --version V1.2.5 --output-dir dist/release
```

源码包只包含 Git 已跟踪文件；`.env`、数据库、证据文件、备份和真实密钥不会进入交付包。
