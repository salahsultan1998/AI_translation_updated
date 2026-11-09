# AI 本地化工作台

Flask 多项目翻译工作台。用户选择项目或「直接翻译」，在对应工作区选择目标语言、翻译文本，以及维护项目术语和翻译记忆。登录可通过环境变量启用或暂时关闭。

## 项目和语言

| 工作区 | 支持的语言 | 原 Supabase 项目标识 |
| --- | --- | --- |
| 直接翻译 | 阿拉伯语、英语、土耳其语 | 不关联项目知识库 |
| Tarbia Baloot | 阿拉伯语 | `Tarbi3a_Baloot` |
| Baloot Challenges | 阿拉伯语 | `Baloot_Challenges` |
| sawa | 阿拉伯语、土耳其语 | `sawa` |
| S06 | 英语 | `S06` |

项目与语言的限制同时在界面和服务器端检查。数据库操作沿用原项目标识及原有的小写规范。DM_03 未列入新应用，未删除数据库中的任何记录。

## 手动输入与 Excel

在项目工作区选择「手动输入」，可从 Excel 复制多行粘贴到表格；列依次为原文、上下文、译文。翻译完成后，单元格仍可编辑。「复制全部译文」复制当前译文列，「复制整表」复制当前三列，可直接粘贴回 Excel。空白行会保留其位置。

如果在翻译运行期间修改或删除了某一行，应用不会用返回结果覆盖该行，而会显示原始结果供参考。下载的文件是翻译服务生成的原始结果，不包含后续在表格中的修改；请使用复制功能取得编辑后的内容。刷新或重新打开工作区会清空当前用户该项目的任务和结果，请先复制或下载。进行中的外部请求可能无法立即停止，且可能产生费用。

## 本地运行

需要 Python 3.11 或更新版本。推荐使用独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # 仅在 .env 不存在时执行，避免覆盖已有密钥
```

在 `.env` 中填写 Supabase URL、公开的 anon/publishable key 和 OpenRouter API key。不要提交 `.env`，不要将 service-role key 当作网页用户凭据。

本地开发时，在 `.env` 中设置 `LOCATOOL_AUTH_ENABLED=false` 可跳过登录。只有明确设为 `false`、`0`、`no` 或 `off` 才会关闭登录；未设置或值无效时默认启用。关闭登录时无需输入账户，但项目知识库仍使用 Supabase key 的匿名权限：如果表权限或 RLS 不允许匿名访问，读取和写入会失败，不能通过关闭登录绕过这些限制。浏览器会话使用各自的任务标识，但这不是身份认证。

在生产环境设为 `LOCATOOL_AUTH_ENABLED=true`，Web 和 worker 必须使用同一设置，并验证 Supabase 登录账户与表权限/RLS 策略。关闭登录时不要把服务公开到互联网。

本地启动时只需运行：

```bash
.venv/bin/python app.py
```

浏览器打开 `http://127.0.0.1:5000`。此本地启动方式会自动启动一个后台 worker；关闭程序也会停止本地 worker。不要把 Flask 内置开发服务器直接公开到互联网。

## 云服务器部署

当前队列为 **单台服务器**设计：Web 进程与独立 worker 共享同一台服务器的本地持久磁盘（SQLite WAL 与私有文件）。这适合约 20 名用户在一台服务器上使用，但实际并发能力取决于文件大小、模型响应时间、OpenRouter 限额与服务器资源，尚未完成真实负载测试。不能将 SQLite 数据库放在不支持其锁语义的网络文件系统上，也不能直接用此方案跨多台服务器分布任务。

建议在 Linux 上使用 HTTPS 反向代理、Gunicorn 和由进程管理器管理的 worker。例如：

```bash
# Web；反向代理转发到本机端口
.venv/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 app:app

# 每个 worker 同时处理一个翻译任务；可根据供应商限额启动 2–4 个进程
.venv/bin/python -m translation_app.jobs
```

Gunicorn 不会启动内置的本地 worker，生产环境仍需单独运行并管理 worker。若 worker 未运行，网页会显示任务正在等待后台处理服务。

使用 systemd、supervisor 或等效工具分别保持 Web 和 worker 进程运行。所有进程必须拥有同一个 `LOCATOOL_STORAGE` 目录，并加载相同的 `.env`；该目录必须可写、仅服务账户可读、位于 Web 静态目录之外。生产环境设置 `LOCATOOL_AUTH_ENABLED=true`，并在使用 HTTPS 时设置 `LOCATOOL_SECURE_COOKIES=true`。设置固定的 `FLASK_SECRET_KEY`，或让所有 Web 进程共享私有目录中自动生成的密钥文件。将 Web 和 worker 的日志接入服务器日志系统，并监控磁盘空间、服务状态及供应商请求限制。

处理期间，任务状态和输入/输出文件暂存在私有目录中；重新打开项目或刷新时清除当前用户该项目的任务和结果。登录 token 在任务等待和运行时也会暂存于私有 SQLite 数据库；删除记录后数据库/WAL 的旧页仍可能包含历史字节。请保护磁盘和备份，并定期检查异常中断后残留的临时文件。

单次请求上限为 25 MiB；XLSX 解压后上限为 200 MiB。工作簿最多 100 个工作表、500 列、共 100,000 条数据行，知识库导入每次最多 10,000 行。限制旨在控制单服务器资源使用，并不代表最大文件已完成负载测试。

当前备份为 `backup_original_20261108/`，包含原始 `.env` 和虚拟环境。不要将备份目录、`.env`、`instance/` 或虚拟环境部署为静态文件或提交到版本库。备份中的虚拟环境可能包含指向原机器的绝对路径，不保证可迁移。

## 为项目添加语言

在项目工作区的「配置」标签中，可以编辑并保存项目提示词和当前语言的提示词；如果该语言有补充规则，也可以单独编辑。直接翻译只显示语言提示词，共享基础规则为只读。保存时若文件已被其他人修改，页面会提示重新加载；每次提交翻译时会固定当时的提示词，之后的修改不会改变已提交的任务。当前关闭登录时，任何能够访问网站的人都可以编辑提示词。

每个项目的语言配置和指令都独立保存在该项目目录。例如 sawa 的阿拉伯语规则位于 `config/projects/sawa/languages/arabic/`，修改该文件不会影响其他项目或直接翻译。

1. 在目标项目的 `languages/` 下创建语言目录，例如 `config/projects/s06/languages/french/`。
2. 参考项目内现有语言创建 `language.json`，设置该项目内唯一的 `id`、中文显示名称、数据库语言名 `database_name` 和 `instructions.md` 路径。已有 Supabase 数据的 `database_name` 应保持不变。
3. 在该语言目录中创建 `instructions.md`，填写此项目和语言的翻译要求。如需在项目通用提示词之后追加更具体的规则，可添加文件并在 `language.json` 中使用 `project_instructions` 指定其相对路径。
4. 在这个项目 `project.json` 的 `languages` 数组中添加语言 ID。无需更改其他项目或 Python 代码。
5. 运行测试后重启 Web 和 worker。配置有误时应用会报告错误。

直接翻译有自己的配置目录 `config/projects/direct/`。如需为它添加语言，也需要在该目录内单独配置；在某个游戏中添加语言不会自动影响直接翻译。

## 添加项目

1. 复制 `config/projects/` 中一个现有项目目录，并改为唯一的小写 ID（只使用小写字母、数字、下划线或连字符）。
2. 修改 `project.json` 的 `id`、显示名称、`database_id`、`languages`、图标路径和提示词路径。`database_id` 必须与已有 Supabase 数据相符；新项目使用新的稳定 ID。
3. 编辑 `prompts/project.md`，为每种支持的语言在项目的 `languages/` 目录下配置独立的语言文件。图标放在 `static/images/` 下，并在配置中使用相对 `static/` 的路径。
4. 验证 Supabase 表权限与 RLS，再运行测试并重启服务。

提示词按顺序由共享基础规则、项目内语言规则、项目通用规则和可选的项目语言补充规则组成；直接翻译使用自己的语言规则，不使用游戏规则。项目数据库中的风格说明、术语及翻译记忆作为参考上下文，用户补充要求加入本次请求。模型仍可能忽略或混淆相互冲突的指令，维护配置时应避免冲突。全局模型选项位于 `config/defaults.json`，敏感密钥仅放在环境变量中。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

自动测试使用模拟认证与模型，不会验证真实 Supabase 策略、云服务器部署或 OpenRouter 模型可用性。上线前应使用测试账户和非敏感样例文件完成一次真实登录、翻译及术语读写验证。

原 Streamlit/FastAPI 文件及旧虚拟环境已移至 `backup_original_20261108/removed_from_workspace_20261108/`；Flask 应用入口为 `app.py`，运行逻辑位于 `translation_app/`。原始完整副本仍保留在备份目录中。
