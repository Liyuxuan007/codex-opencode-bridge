# Codex ↔ WSL OpenCode 桥接

[English and complete tool reference](README.md) · [安全说明](SECURITY.md)

使用 Python 标准库，将 Codex 的任务经 MCP 交给 WSL 中的常驻 OpenCode。
多个 Codex 调用方共用 SQLite 队列和全局并发上限（默认 2），每个任务使用独立
会话，并行写任务使用独立 Git worktree。桥接不复制或转换订阅凭证，也不绕过额度。
本项目不是 OpenAI 或 OpenCode 的官方产品。

## 默认协作流程（1.3.0）

建议一个 Codex 协调者、两个 OpenCode 执行槽位，小任务分批排队；确有独立工作时
再考虑三个。模型及思考强度保持不变。完整任务模板与验收流程见 [WORKFLOW.md](WORKFLOW.md)。

- `job_submit` 可传 `brief`（goal、interfaces、acceptance、stop_conditions）和
  `risk`（low、standard、high），明确文件归属、接口、失败路径与停止条件。
- 执行者按 STATUS / CHANGES / CHECKS / RISKS / DECISIONS 简短交付；这是提示词约定，
  不代表输出经过结构化校验或代码已正确。
- 状态、结果、等待及验证默认精简；`view="full"` 按需读取完整视图。结果默认 2500
  字符，检查输出默认每流 2000 字符，成功验证每流最多 256 字符，截断均明确标记。
- 模板启用 `require_verification=true`；先运行独立验证，再按风险审查补丁，最后串行
  应用及集成检查。已有私有配置需明确更新，不能靠拉取模板自动生效。
- 计量导出器可用 `--assessment` 加入人工记录的首次验收、返工、集成失败和墙钟时间；
  未测量为 null，不以执行 succeeded 推断验收通过，不把并行执行耗时相加。

当前无自动修复派工，也无同一会话追加任务。修复须明确创建新任务、新 request_id，
使用审阅过的已提交基线；恢复监控不会重发提示词。尚未实测宣称提速或节省百分比。

## 安装

要求 WSL2、Python 3.10+、Git、已登录的 OpenCode CLI，以及 Windows Codex CLI。
已验证环境：Ubuntu 24.04、OpenCode 1.19.2、Codex CLI 0.163.0-alpha.10.2。
桥接版本 1.3.0。默认模型示例为 `opencode-go/deepseek-v4.1-flash`，具体 revision
由提供商管理；安装前确认账户可用模型。代码不包含模型权重、数据集或向量库。

1. 克隆仓库到 WSL 可访问的位置。
2. 复制 `config.example.json` 为 `config.json`，编辑 WSL 发行版、OpenCode
   可执行路径、模型及项目根目录。默认仅允许 `~/projects`，`~` 在 Linux 中展开。
   如需 Windows 项目，仅增加对应的、尽可能窄的 WSL 项目目录。
3. 在 WSL 中完成 OpenCode 登录并检查模型访问权限。
4. 在 Windows PowerShell 的仓库目录运行：

```powershell
.\Install.ps1
.\Bridge.ps1 -Action doctor
.\Bridge.ps1 -Action models
.\Bridge.ps1 -Action test
```

安装器自动识别 WSL 用户目录，运行文件安装到 `~/.local/share/codex-opencode-bridge`，
私有状态保存在 `~/.local/state/codex-opencode-bridge`。安装器会备份已有 Windows
Codex 配置并注册 MCP；重新打开 Codex 聊天即可加载工具。升级前完成或取消所有任务，
保留自己的本地配置并手动审阅模板新增字段。Linux Codex 的安装命令见英文文档。

`config.json` 不入库；认证继续由各 CLI 管理。`.env.example` 仅说明可选的
`CODEX_OPENCODE_STATE` 环境变量，程序不会自动读取 `.env`，不要在这里填 API key。

## 使用与效率

1.2.0 新增受控模型目录刷新、执行配置、provider/model 限流、恢复工具、任务契约、
独立验收、带摘要的补丁整合和清理预览。完整参数见[英文文档](README.md)与
[更新记录](CHANGELOG.md)。默认仍是现有 Go provider 和六路全局上限，不自动切换付费模型。

推荐流程：`job_submit(contract=...)` → 集中 `job_wait` → `job_verify` → 审查
`job_diff` 与验收结果 → `job_apply(patch_sha256=已审查摘要)`。契约包含允许/禁止路径、
验收命令和文件数量上限；有契约的任务要求验收通过且工件摘要仍一致。无检查不会算通过。
原有无契约任务保留兼容模式，也可显式配置 `require_verification=true` 强制独立验收。
摘要覆盖未忽略的工作区文件，不代表外部服务、依赖与忽略配置已被冻结。

`job_recover` 可检查、请求中止或重新挂接已有会话；不会重放 prompt。
`job_cleanup` 默认只预览，保护未整合或已变化的成果，不自动删除原生会话或历史任务。
回问需全局与每任务同时选择开启，属实验功能；等待期间继续占用配额，不承诺自动释放槽位。
task 工具仍禁用，执行者不再创建执行者。当前不自动派发修复任务。

`scripts/measure_usage.py` 可记录选定 Codex 轮次与两个 worker 的实际计量，去重逐次响应，
不导出 prompt、工具参数或本地会话标识。原始日志和生成报告仍须保存在仓库外或忽略路径。
可用后台等待完成选项在本轮结束后补齐最终答复用量；尚未记录的用量会标注为快照。

在新 Codex 聊天指定已允许的绝对项目路径，要求先检查 `bridge_health`、
`bridge_models` 和 `project_prepare`，建立可运行的最小脚手架及干净的 Git 基线。
按可独立验收的小任务调用 `job_submit`，使用稳定且唯一的 `request_id` 和明确的
`owner`，并行写任务选 `worktree`。由主 agent 集中批量 `job_wait`，子 agent
提交后返回 ID，结果到达再恢复审查，避免各层反复轮询。

使用 `job_result` 检查答案和累计用量、`job_diff` 审查补丁、`run_check` 进行独立
验证，确认后 `job_apply`。后者不会提交或推送。确定性的测试直接运行命令，不必
额外调用模型。`succeeded` 仅表示模型完成答复，不代表代码已通过验收。

`exclusive` 模式直接修改项目，与同一项目的任务互斥。`readonly` 禁止编辑和 shell
工具，但它只是工具策略。worktree、允许目录均不是操作系统沙箱，OpenCode 不会
自动继承 Codex 的权限边界。写任务及 `run_check` 以 WSL 用户权限执行。

进度超过 120 秒没有可观察变化时提示停滞，不自动杀死或重放任务。超时保留部分结果，
不确定的提交不盲目重试；中止状态不明时保留槽位并提示人工检查。发生崩溃时不能保证
工具副作用恰好执行一次。累计 usage 不是订阅账单，也不包含所有 Codex 主控用量。

## 验证和上传防护

离线回归：`python3 -m unittest discover -s . -p 'test_*.py' -v`。发布策略测试：
`python3 -m unittest discover -s scripts -p 'test_*.py' -v`。
`verify_live.py` 会使用六个模型任务并保留本地测试项目及报告，应只在有额度、配置正确
且共享服务空闲时主动运行，CI 不调用模型。`--resume` 可复核已有报告。

采用默认拒绝的 `.gitignore`；新增公开文件必须同时审阅并更新发布扫描器的文件白名单。
本地配置、Agent 状态、凭证、日志、prompt、数据库、补丁、验收报告、模型及数据均
不上传。安装 Gitleaks 后运行 `python3 scripts/install_hooks.py`，或安装 pre-commit
钩子；CI 再次检查。推送前仍须人工审阅暂存差异，并确认 GitHub 的 secret scanning
和 push protection 已启用。扫描通过不等于能够发现所有敏感信息。

忽略文件仍可能被 agent 读取。不要把生产密钥放进工作目录，不要在公开 issue 中粘贴
原始日志、会话或补丁。更完整的权限和保留数据说明见 [SECURITY.md](SECURITY.md)。

尚未选择开源许可证；公开可见不等于授予通用复用许可，仓库所有者可另行添加许可证。
