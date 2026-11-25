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

## 项目知识库

在工作区打开「项目知识库」，选择「术语库」或「历史翻译」。两页均可查原文与译文、按场景及状态筛选、手动新增、粘贴 Excel 行、导入 CSV/XLSX，并导出筛选后的 XLSX。历史翻译可额外通过字符串 Key 检索。项目和目标语言限定读写范围。

- 手动新增默认「可用」。批量粘贴和文件导入先预览新增、相同、冲突、无效行，确认后只保存新增项，初始状态为「待确认」。
- 「可用」条目进入翻译参考。「待确认」和「已归档」条目仍可管理。已有记录迁移后继续作为参考，历史译文经编辑或明确设为「可用」后才有资格直接复用。
- 复选框可选择多条，表头只全选本页。翻页保留选择，切换知识库、语言或筛选条件会清空选择。批量改状态可用于启用、暂停和归档，批量删除需要确认。任一条目的版本过期时，数据库拒绝整批操作。
- 场景限定词条的适用范围。同一原文可保留不同场景或不同字符串 Key 的历史译文。在翻译行的「上下文」内填写相同的场景或字符串 Key，可获得对应参考。

### 现有 Supabase 项目的升级

先备份 Supabase，并暂停旧版写入。在已有 `public.glossaries` 和 `public.translation_memory` 的环境运行 [知识库迁移](supabase/migrations/20261125093234_knowledge_management.sql)。迁移复制两张表的原有条目，包括未挂载的 DM_03，并保留原表供管理员核对。新版 Web、Worker 和外部 API 一起切换。原表的匿名及普通用户授权会被收回。

项目权限由管理员在 `public.knowledge_project_members` 配置。使用旧知识库的项目数据库 ID（小写），如 `tarbi3a_baloot`、`baloot_challenges`、`sawa`、`s06`。编辑账号使用 `can_edit=true`，仅查询或外部翻译 API 账号使用 `false`。示例中请替换项目和邮箱。多个项目分别授权。

```sql
insert into public.knowledge_project_members (game_id, user_id, can_edit)
select 'tarbi3a_baloot', id, true from auth.users where email = 'translator@example.com'
on conflict (game_id, user_id) do update set can_edit = excluded.can_edit;
```

切换前核对新旧表条目数量，并用编辑账号、只读账号和无权限账号验证访问。需要立即撤销某个账号的翻译缓存访问时，部署环境用 `TRANSLATION_RESOURCE_CACHE_SECONDS=0` 重新验证权限。

## 本地运行

需要 Python 3.11 或更新版本。推荐使用独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # 仅在 .env 不存在时执行，避免覆盖已有密钥
```

在 `.env` 中填写 Supabase URL、公开的 anon/publishable key 和 OpenRouter API key。不要提交 `.env`，不要将 service-role key 当作网页用户凭据。

本地开发时，在 `.env` 中设置 `LOCATOOL_AUTH_ENABLED=false` 可跳过登录。只有明确设为 `false`、`0`、`no` 或 `off` 才会关闭登录；未设置或值无效时默认启用。关闭登录时可使用「直接翻译」。项目知识库及使用它的项目翻译需要登录，并由 Supabase 验证项目权限。浏览器会话使用各自的任务标识。

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
.venv/bin/gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 2 app:app

# 2 核 / 4 GiB 先启动 1 个翻译 Worker，观察内存和排队时间后再调整
.venv/bin/python -m translation_app.jobs
```

Gunicorn 不会启动内置的本地 worker，生产环境仍需单独运行并管理 worker。若 worker 未运行，网页会显示任务正在等待后台处理服务。

使用 systemd、supervisor 或等效工具分别保持 Web 和 worker 进程运行。所有进程必须拥有同一个 `LOCATOOL_STORAGE` 目录，并加载相同的 `.env`；该目录必须可写、仅服务账户可读、位于 Web 静态目录之外。生产环境设置 `LOCATOOL_AUTH_ENABLED=true`，并在使用 HTTPS 时设置 `LOCATOOL_SECURE_COOKIES=true`。设置固定的 `FLASK_SECRET_KEY`，或让所有 Web 进程共享私有目录中自动生成的密钥文件。将 Web 和 worker 的日志接入服务器日志系统，并监控磁盘空间、服务状态及供应商请求限制。

处理期间，任务状态和输入/输出文件暂存在私有目录中；重新打开项目或刷新时清除当前用户该项目的任务和结果。登录 token 在任务等待和运行时也会暂存于私有 SQLite 数据库；删除记录后数据库/WAL 的旧页仍可能包含历史字节。请保护磁盘和备份，并定期检查异常中断后残留的临时文件。

单次请求上限为 25 MiB；XLSX 解压后上限为 200 MiB。工作簿最多 100 个工作表、500 列、共 100,000 条数据行，知识库导入每次最多 10,000 行。限制旨在控制单服务器资源使用，并不代表最大文件已完成负载测试。

## 开发平台翻译 API

```bash
.venv/bin/uvicorn translation_app.http_api:app --host 127.0.0.1 --port 8001
```

需要在浏览器里测试时，在启动命令前加 `LOCATOOL_API_DOCS=true`，然后打开 `http://127.0.0.1:8001/docs`。文档默认关闭。

将原 API 地址的 `POST /v1/translate` 转发给此进程，调用请求和成功响应沿用旧格式。传入 `game_id`、`target_language` 和 `texts`，`texts` 包含一条或多条记录。每条可传 `id` 和 `context`，译文按原顺序返回并带回原 ID。可选项包括 `model`、`batch_size`、`use_smart_budget` 和 `custom_instructions`。知识库和校验始终启用，旧请求可继续传 `validate: true` 和 `use_rag: true`。备用模型沿用服务器的 `OPENROUTER_FALLBACK_MODEL`。

沿用已有的 `LOCATOOL_API_KEY`，调用方通过 `X-API-Key` 发送。生产环境在 API 服务端配置 `LOCATOOL_API_SUPABASE_EMAIL` 和 `LOCATOOL_API_SUPABASE_PASSWORD`，使用对提示词、术语和翻译记忆有读取权限的普通 Supabase 账号。现有 Supabase 用户也可用其访问 Token 作为 Bearer 凭据。Flask、FastAPI 和 Worker 使用同一份代码、Supabase 配置和 `LOCATOOL_STORAGE`。网页保存术语或历史翻译会使共享缓存失效，保存提示词后新请求会直接读取新内容。

接口同步等待结果，服务端最多等待 120 秒，超过时返回 HTTP 504。调用方读取超时和反向代理等待上游响应的时间应长于 120 秒，以便收到接口明确的超时响应。超时后，后台停止派发新批次，已发出的模型请求可能继续到结束。

### 导出最新术语库

向同一个 API 地址发送 `POST /v1/glossary`，沿用 `X-API-Key`，请求正文示例为 `{"game_id":"tarbi3a_baloot","target_language":"arabic"}`。如果反向代理只转发了翻译路径，也要转发 `/v1/glossary` 到同一个 FastAPI 服务。

接口每次读取所选项目和语言的全部「可用」术语。成功响应包含 `game_id`、`target_language`、`count`、`terms`。`terms` 是数组，每条含 `source`、`target`、`scene`、`note`。空场景表示通用，无可用术语时返回 `count: 0` 和 `terms: []`。服务端 Supabase 账号有项目只读权限即可。缺少有效凭据返回 `401`，缺少项目权限返回 `403`。

`POST /v1/history` 沿用同样的请求正文、认证方式和权限，返回全部「可用」历史翻译。响应的数组字段是 `history`，每条额外包含 `string_key`。无记录时返回 `count: 0` 和 `history: []`。本地示例见 [test_API_history.http](test_API_history.http)。

## 翻译性能与失败恢复

翻译请求显式发送 `reasoning: {"enabled": false}`，关闭模型内部推理以降低翻译延迟；重试和备用模型请求沿用此设置。请求开始日志包含 `reasoning_enabled=False`。变更此设置会使旧翻译检查点失效，避免混用不同请求配置的结果。实际耗时仍受模型与供应商响应影响。

每个任务默认最多同时处理 50 个模型请求，同一服务器所有 Worker 合计最多 50 个在途 HTTP 请求。通过共享本地目录中的文件锁协调，进程退出会释放槽位；适用于 macOS/Linux。Web 和所有 Worker 必须使用相同存储目录与全局限制；修改限制后应统一重启所有 Worker。增加 Gunicorn 进程不会增加翻译 Worker。可在 `.env` 配置：

| 配置 | 默认值 | 范围与用途 |
| --- | --- | --- |
| `TRANSLATION_CONCURRENCY` | 50 | 1–128，每任务最大并发 |
| `TRANSLATION_GLOBAL_CONCURRENCY` | 50 | 1–128，全服务器在途请求上限 |
| `TRANSLATION_INPUT_TOKEN_BUDGET` | 6000 | 256–64000，包含提示词、参考、补充要求和源文的估算预算 |
| `TRANSLATION_OUTPUT_TOKEN_BUDGET` | 4000 | 256–32000，估算输出预算，同时作为模型 `max_tokens` |
| `TRANSLATION_RESOURCE_CACHE_SECONDS` | 300 | 0–3600，知识库缓存时长；0 禁用 |

每请求的行数与同时在途的请求数分别配置：默认每请求最多 40 行，最多 50 个请求同时执行。有至少 50 个待处理批次时才可能达到 50 并发。Token 预算充足时，50 条互不相同的短文本会分成 40 条和 10 条两批。实际还受供应商限额和可用批次数影响。

在 `.env` 修改以下数值后，等当前任务结束并统一重启 Web 和 Worker；已有环境变量会覆盖代码默认值。2 核 / 4 GiB 可继续使用 **1 个 Web 进程（2 个线程）+ 1 个翻译 Worker**。50 并发未在真实模型服务上压测，需观察 429、请求耗时及内存；可降低为 5 或 10。

```dotenv
TRANSLATION_CONCURRENCY=50
TRANSLATION_GLOBAL_CONCURRENCY=50
TRANSLATION_RESOURCE_CACHE_SECONDS=300
```

默认每批最多 40 行，只为即将发送的批次生成参考上下文，避免整份文件的上下文同时驻留内存。自动分批根据估算输出量组批，完整参考上下文拼接后再检查输入、输出预算；手动模式也执行预算检查，必要时拆小。Token 使用启发式估算，并非模型的精确分词；目标语言膨胀、变量较多时仍可能触及模型输出上限。单行超出预算时会提示缩短内容，不会擅自切断语义。调整预算必须考虑所选模型的实际限制，并用样本检查漏行、标签和译文质量。预算按批次检查，后续行超限时前面的请求可能已完成并计费，成功行仍会保存到检查点。

同一任务内，原文和上下文都相同的行会跨工作表复用译文；不同语境请填入上下文。高级设置中的「直接复用完全匹配的翻译记忆」默认关闭，启用后只复用已确认的唯一通用记忆，且需原文和变量标签完全匹配。本次有补充要求、该行有上下文或有适用术语时，仍交给 AI。快捷复用会跳过模型的风格调整。

排队或翻译中会显示「停止翻译」按钮。排队任务立即停止且不会被 Worker 领取；运行中的任务进入「正在停止」，Worker 停止派发新批次、等待并发槽位及重试。已发到供应商的 HTTP 请求无法保证撤回，可能继续处理和计费；它们返回的有效结果仍会保存到检查点，全部收尾后状态变为「已停止」。停止期间不能继续同一任务，完成收尾后可点击「继续未完成的翻译」。取消接口为 `POST /api/jobs/<id>/cancel`，按任务所有者及 CSRF 校验；已完成的任务不会被晚到的停止请求覆盖。

每个成功且通过校验的批次会保存到任务私有目录的检查点。失败、中断或停止后，可在当前页面点击「继续未完成的翻译」，已保存的成功行不会重发。原始模型、提示词和要求被冻结，当前登录凭据会更新；源文、配置或知识库内容变化时检查点会失效，可能重新翻译所有行。超时、中断的请求可能已在服务端完成并计费，继续翻译可能重复计费；读取超时不会自动重试。失败时不提供部分结果文件。刷新、离开页面仍按原有规则清除任务，需在清理前继续或下载。

进入项目工作区时会自动预加载当前语言的术语和翻译记忆；切换语言以及通过本工具修改知识库后会重新预加载。页面会显示加载中、已就绪或失败状态；若立即开始翻译，会等待正在进行的预加载完成。直接翻译不预加载项目知识库。

知识库缓存默认保留 300 秒，Web 和独立 Worker 使用私有本地 SQLite 缓存共享已加载数据，不需要分别查询 Supabase。磁盘最多保留 32 个有效快照，内存每进程最多保留 2 份；按需只预加载当前语言，避免一次加载所有语言。设置 `TRANSLATION_RESOURCE_CACHE_SECONDS=0` 会禁用复用，翻译时将重新读取知识库。缓存按 Token、数据库、项目、语言隔离；JWT 到期后不再使用，无法解析的 Token 不缓存。通过本工具修改知识库时会使各进程缓存失效；直接在 Supabase 编辑数据或撤销权限，最多可能等待缓存 TTL，要求立即重新验证时将缓存设为 0。共享缓存与任务使用相同私有存储目录，完整知识库只保存在服务端，预加载接口只返回条数及有效期；过期、登录 Token 变化或知识库更新后会重新加载。每个任务的恢复指纹包含本次知识库快照。HTTP 连接按线程复用，不共享用户数据库客户端。

任务状态区显示读取、加载知识库、分批、翻译和生成文件阶段。计数为去重后的行数，文件生成完毕才显示 100%。状态 API 的 `summary` 包含读取、资源加载、准备、翻译和写出耗时，以及去重、记忆命中、恢复行数、HTTP 尝试数、429 次数、请求耗时 P95、等待全局并发槽位耗时和供应商返回的 Token 用量；`queue_seconds` 单独记录本次排队时间。上下文匹配和逐批预算检查计入翻译阶段；准备阶段记录检查点指纹和恢复等初始工作。请求耗时是跨并发请求累计值，可能大于任务墙钟时间。日志记录各阶段耗时。调试时默认打印完整 system/user 提示词（含原文和参考上下文），并显示字符数与估算输入 Token 数；设置 `TRANSLATION_LOG_PROMPTS=false` 可关闭完整内容，只保留长度统计。不会打印 API 密钥或登录凭据。

可运行离线模拟比较并发和批量设置；它验证调度收益与行号写回，不调用网络，也不代表真实模型提速或译文质量：

```bash
.venv/bin/python scripts/benchmark_translation.py
```

当前备份为 `backup_original_20261108/`，包含原始 `.env` 和虚拟环境。不要将备份目录、`.env`、`instance/` 或虚拟环境部署为静态文件或提交到版本库。备份中的虚拟环境可能包含指向原机器的绝对路径，不保证可迁移。

## 提示词配置与添加语言

生产环境从 Supabase 的 `public.prompt_configs` 读取两份提示词，主键为 `(project_id, target_language, kind)`。`project_id` 和 `target_language` 使用仓库中的小写 ID，例如 `direct` / `english`，而非知识库的名称。`kind` 为 `system` 或 `instructions`。

仓库文件用作本地开发的只读默认值，也可用于初始化新的项目语言：
```text
config/projects/sawa/languages/arabic/
  language.json
  system.md          # System prompt：角色、核心规则、输出要求
  instructions.md    # Instructions：游戏风格、语气及此语言的翻译要求
```

开启登录后，团队成员通过工作区「配置」读取和保存共享提示词。保存只更新 Supabase；部署或拉取 Git 更新不会覆盖数据库内容。此权限模型用于关闭公开注册的 Supabase 项目。关闭登录仅用于本地开发，「配置」显示只读文件内容。生产环境缺少提示词或 Supabase 请求失败时会报错，避免翻译任务使用错误的规则。

各项目、各语言使用独立的提示词，修改 Sawa 阿拉伯语不会影响 Sawa 土耳其语或其他项目。Python 仍验证 JSON、行号和变量标签，删除 system 提示词中的必要规则可能导致返回内容未通过校验。

实际请求的 `system` 消息使用系统提示词原文，`user` 消息包含翻译要求原文，以及目标语言、匹配的术语/翻译记忆、源文本和行上下文。高级设置中的「补充翻译要求」仅用于本次任务，会加入同一条 user 消息，并保存在该任务的快照中供重试使用。

多人同时修改同一提示词时，保存会检查数据库版本；发生冲突会返回 409 并要求重新加载。提交翻译时会冻结两份提示词；之后的编辑只影响新任务，失败重试仍使用原任务保存的内容。

为项目添加语言：

1. 在项目 `languages/` 下建立目录，例如 `config/projects/s06/languages/french/`。
2. 创建 `system.md` 与 `instructions.md`，按新语言填写完整规则；它们不会继承其他文件。
3. 创建 `language.json`，例如：

   ```json
   {
     "id": "french",
     "name": "法语",
     "database_name": "French",
     "system_prompt": "system.md",
     "instructions": "instructions.md"
   }
   ```

4. 将语言 ID 加入本项目 `project.json` 的 `languages` 数组。已有 Supabase 数据的 `database_name` 保持原值。
5. 部署前由 Supabase 管理员向 `prompt_configs` 添加两条对应记录，分别使用 `kind = 'system'` 和 `kind = 'instructions'`，并填写 `content`。空文本也是有效内容。验证配置并运行测试。直接翻译也需要独立配置。

## 添加项目

1. 复制 `config/projects/` 中现有项目目录，改为唯一小写 ID（字母、数字、下划线或连字符）。
2. 修改 `project.json` 的 `id`、显示名称、`database_id`、`languages` 和图标路径。`database_id` 与已有 Supabase 项目保持一致。
3. 独立编辑每种语言的 `system.md` 和 `instructions.md`；项目不再有 `project_prompt` 配置。
4. 在 Supabase 为每种语言初始化两条提示词记录并验证权限，再运行测试并重启服务。

全局模型选项位于 `config/defaults.json`，敏感密钥仅放在环境变量中。提示词内容本身允许团队自由编辑；应用不保证模型一定遵循所有规则，请保持说明清晰并避免冲突。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

自动测试使用模拟认证与模型，不会验证真实 Supabase 策略、云服务器部署或 OpenRouter 模型可用性。上线前应使用测试账户和非敏感样例文件完成一次真实登录、翻译及术语读写验证。

原 Streamlit/FastAPI 文件及旧虚拟环境已移至 `backup_original_20261108/removed_from_workspace_20261108/`；Flask 应用入口为 `app.py`，运行逻辑位于 `translation_app/`。原始完整副本仍保留在备份目录中。
