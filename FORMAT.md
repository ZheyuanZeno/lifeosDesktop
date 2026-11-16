# LifeOS 模块白板数据格式 · 版本 2

## 存储

`lifeos.sqlite` 是主资料库。包含 modules（模块定义）、records（完整记录）、metadata（白板设置）、inbox（原始收件箱）和 pomodoros（番茄钟状态及历史）五个表。一个模块对应一个逻辑记录集合，它们共享同一个数据库文件；并不是每增加模块都建立一个独立数据库文件。所有内容一次事务保存。

`records.json` 是 SQLite 同步导出的 UTF-8 快照。`days/*.md` 是按日期生成的阅读副本。二者不会回写数据库；如需修改，请在应用编辑或通过导入更新。旧版 `index.sqlite` 不再参与工作。

## 备份与迁移

完整 ZIP 包含：

- records.json：所有模块、属性、记录、番茄钟历史和白板设置，包括回收站数据。
- 阅读我的记录.html：按日期阅读，附件内嵌，可脱离软件打开。
- days/：每日 Markdown，附件使用相对路径。
- modules/模块编号/module.json：模块定义。
- modules/模块编号/records.csv：该模块的可见记录，包含自定义属性列。
- expenses.csv、tasks.csv：全部支出与计划的表格导出。
- attachments/：所有被记录关联的原始文件，包括回收站中的关联。

每个模块也可以单独导出 ZIP、CSV。单模块导出中可能包含指向其他模块的原始日记编号；完整关系恢复请备份整个白板。HTML 和 CSV 不包含回收站记录；归档模块及其未删除记录仍包含在完整备份里。

在空资料库导入完整 ZIP 可恢复模块顺序、属性、正文、状态和附件。合并到非空资料库时，默认跳过已存在编号；勾选更新则更新相同编号，但不会删除目标库中其他内容。完整布局复原推荐导入空库。

也可以关闭应用后复制整个 data 目录。只复制运行中的数据库文件可能不完整。程序打包中的 JSON 会在首次启动时迁移为 SQLite。

## JSON 顶层

- schema_version：2。支持导入旧版 1。
- board_title：白板名称。
- updated_at：修改时间，带 UTC 时区。
- modules：按白板顺序排列的模块数组。
- records：全部记录数组。
- inbox：原始收件箱条目数组。
- pomodoros：番茄钟状态和历史数组；运行中的计时可在刷新后恢复。

## 模块

- id：稳定唯一编号，仅英文、数字、下划线、连字符。
- name：自定义名称。
- kind：journal（日记）、health（健康）、shopping（购物）、plans（计划）、custom（空白）。
- view：list 或 table。
- archived：是否收起到回收站。收起模块不删除其记录。
- layout：模块的画布位置和固定尺寸，包含 x、y、width、height，单位是画布像素；允许负坐标，备份恢复后保持位置。

画布视角的平移与缩放保存在当前浏览器，和数据库中的模块位置分别保存。
- fields：属性定义，每项包含 id、name、type；type 为 text、number、date。

模块的日期、名称、正文为标准字段；购物还有金额和币种，计划还有完成状态。fields 添加额外属性。

## 记录

- id：稳定唯一编号；修改时保持不变。
- module_id：所属模块编号。新导入模块必须同时提供定义。不提供模块编号时按类型匹配或建立默认模块。
- type：journal（日记）、activity（完成事项）、expense（支出）、task（计划）、note（普通记录）。
- date：YYYY-MM-DD 归属日期。计划使用预定日期。
- title、body：标题与正文；日记保留原文。
- status：todo 或 done。
- amount：非负十进制字符串，最多两位小数；未知为 null，不是零。金额已知时 currency 必填。
- currency：币种，不混加、不自动换算。
- time_label：原始时间说明，允许为空或“课后”等模糊描述。
- source_id：关联原始记录编号，可为空。
- properties：属性编号到文字值的对象。数字和日期也以字符串存储，未知留空。
- attachments：列表，每项是 {file: UUID格式相对文件名, name: 原始文件名}。
- created_at、updated_at：带 UTC 时区的保存时间。
- deleted：是否在记录回收站。

## 限制

本机运行，无云同步、自动识别、自动提醒或数据库加密。上传附件保存原文件，不等于自动分类或识别。单个附件最多 10MB；网页导入文件最多 30MB，ZIP 解压最多 100MB。更大的资料可停机后复制完整目录。

最近 30 次操作保留 JSON 快照，可逐次撤销。迁移前的原始 JSON 另行保留，不参与普通撤销。定期把完整备份保存到另一块磁盘。

CSV 使用 UTF-8 BOM；可能被表格软件当作公式的文字会加单引号。精确原文以数据库或 JSON 为准。
