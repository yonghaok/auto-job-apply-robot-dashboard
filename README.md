# resume-bot · 一键自动填简历机器人

> GitHub 仓库：<https://github.com/yonghaok/auto-job-apply-robot-dashboard>
> 项目内部代号 `resume-bot`（模块名、目录名沿用），与仓库名不同名，属正常。
> 下文所有路径均指仓库根目录。

把求职网站的申请表单**自动填到「提交前」**，由你核对后自己点提交。

不是批量海投工具：它**刻意不点提交按钮**，也不绕过验证码。定位是「把重复的录入工作干掉，
把判断权留给人」。

> **免责声明**
> 本项目仅供个人求职使用，需在本机运行。使用者需自行遵守目标网站的《用户协议》与
> robots 规则；因使用本工具产生的账号风险、数据合规责任由使用者承担。
> 仓库不包含任何真实个人信息、简历文件或投递记录。

---

## 1. 它能做什么

| 能力 | 说明 |
|---|---|
| 岗位表导入 | 读 `岗位总表.xlsx`，一行一个岗位，按公司自动分栏 |
| 条件筛选 | 关键词全含（严格）/ 任一含（宽松）+ 城市命中，两种口径实时切换 |
| 详情页扫描 | Playwright 并发抓取岗位页，缓存结果，可断点续扫 |
| 表单自动填写 | 按「字段别名表 + 逐一配对」把个人信息填入 input/textarea/select/radio/文件上传 |
| 复杂经历区块 | 自动点「添加」建行，逐条填实习、项目（支持 3 条并排卡片式布局） |
| 日期控件 | 适配 Phoenix/UD 日历面板，自动翻年、点月、输入日 |
| 短信验证码 | 配套 Android App 把手机收到的验证码转发到 PC，看板直接显示 |
| 停在提交前 | 填完即停，浏览器窗口保持打开，等你核对 |

已验证的站点组件体系：

| 站点体系 | 典型站点 | 关键特征 |
|---|---|---|
| 飞书招聘（Formily + UD） | 蔚来等 | `.ud-formily-item`、`.ud__select`、`apply-form-array-card` |
| 北森 Beisen（Phoenix） | 虹软等 | `.form-item__text`、`.phoenix-select`、`.phoenix-calendar-panel` |
| MokaHR | 鹰角网络等 | SPA 路由 `#/job/<id>/apply`，列表页与会详情页需区分 |

其它标准表单会走通用别名表兜底；未命中的字段会如实列在「未填清单」里，交人工补。

---

## 2. 架构

```
resume-bot/
├── backend/                 Python 后端（零 Web 框架，标准库 http.server）
│   ├── server.py            路由 / 看板 API / 任务队列 / 看门狗
│   ├── filler.py            表单采集与填写引擎（Playwright，确定性匹配，不调用 LLM）
│   ├── positions.py         岗位分栏聚合与筛选口径（纯函数）
│   ├── candidates.py        岗位表读取 → 候选任务
│   ├── captcha.py           滑块/图形验证码的人工接管入口
│   ├── edge_cdp.py          接管本机 Edge/Chrome 调试实例（复用登录态）
│   ├── state.py             投递状态持久化
│   └── smoke_*.py           回归测试（不依赖网络，可离线跑）
├── frontend/
│   └── index.html           单页看板（无构建步骤，原生 JS）
├── android/                 短信验证码转发 App（Kotlin）
│   └── dist/*.apk           已构建的安装包
└── 岗位总表.xlsx             运行时数据（不入库，见 .gitignore）
```

数据流：

```
岗位总表.xlsx ──► candidates.py ──► 看板（分栏 + 筛选）
                                      │
                             点击「一键投递」
                                      ▼
                    filler.py 打开详情页 → 采集控件 → 别名匹配 → 逐项填写
                                      │
                          遇到登录/验证码 ──► 手机短信 ──► Android App ──► POST /sms
                                      │
                                      ▼
                          填完停止，窗口保持打开（等你核对后手动提交）
```

---

## 3. 快速开始

### 3.1 获取代码

```bash
git clone https://github.com/yonghaok/auto-job-apply-robot-dashboard.git
cd auto-job-apply-robot-dashboard
```

也可以直接在仓库页面点 `Code → Download ZIP`（不装 git 也能用），解压后进同名目录即可。

### 3.2 环境要求

- Windows 10/11（主要验证平台；macOS/Linux 需自行调整路径）
- Python 3.9+
- 本机已安装 **Chrome**（默认用 `channel="chrome"`；可用 `RESUME_BROWSER_CHANNEL=edge` 切 Edge）

### 3.3 安装依赖

```bash
python -m pip install openpyxl playwright
# 如果本机没有 Chrome，可让 Playwright 自带的 Chromium 兜底：
python -m playwright install chromium
```

### 3.4 准备岗位表

把职位信息整理成 `岗位总表.xlsx`，放在**仓库根目录**（与 `backend/`、`frontend/` 同级）。

至少需要这三列：

| 列名 | 必填 | 说明 |
|---|---|---|
| `公司` | ✅ | 看板按此列分栏 |
| `岗位名称` | ✅ | 卡片标题 |
| `岗位页链接` | 建议 | 详情页 URL；为空则该岗位无法扫描与投递 |

可选列：`城市`（地域筛选）、`职责族`（分组标签）、`来源校招页`（溯源）。

列名不必完全一致 —— 安装 `skills/job-sheet-adapter` 后，可让 AI agent 自动把任意格式的
Excel 转成这个标准结构并按条件筛选，见第 7 节。

### 3.5 启动

```bash
# 在仓库根目录
python backend/server.py
```

浏览器打开 <http://127.0.0.1:8000>。

> 首次启动若报 `FileNotFoundError: ...岗位总表.xlsx`，说明岗位表还没放到位。
> 也可用环境变量指定路径：`set RESUME_XLSX=D:\path\to\your.xlsx`（PowerShell 用 `$env:RESUME_XLSX=...`）。

---

## 4. 使用流程

### 第 1 步：填个人信息

点右上角「个人信息」，一次性录入：

| 字段 | 说明 |
|---|---|
| 姓名 / 性别 / 手机 / 邮箱 / 出生日期 | 基础信息 |
| 所在城市 / 意向城市 / 籍贯 / 现居地址 / 学校地址 | 地址类字段 |
| 身份证号 | 部分国企/央企表单必填 |
| 是否接受调剂 | 布尔值 |
| 学校 / 专业 / 学历 / GPA / 起止时间 / 毕业时间 | 教育背景 |
| 简历 PDF 路径 | 本机绝对路径，投递时用于文件上传 |
| 自我介绍 | 长文本 |
| 实习经历 / 项目经历 / 技能 / 获奖 | 每行一条，程序会按行拆成多条区块 |

保存后写入 `backend/profile.json`（**已在 .gitignore 中，不会被提交**）。

也可直接编辑 `backend/profile.json`，参考结构：

```json
{
  "name": "张三",
  "gender": "男",
  "phone": "13800000000",
  "email": "zhangsan@example.com",
  "birth": "2000-01-01",
  "location": "杭州",
  "intended_city": "杭州、上海",
  "native_place": "浙江杭州",
  "current_addr": "杭州市西湖区",
  "school_addr": "杭州市钱塘区",
  "id_card": "330100200001011234",
  "accept_adjust": true,
  "education": {
    "school": "某某大学",
    "major": "电子信息工程",
    "degree": "本科",
    "gpa": "3.6/4.0",
    "period": "2023-09 ~ 2027-06",
    "grad": "2027-06"
  },
  "resume_pdf": "C:/Users/you/Documents/resume.pdf",
  "self_intro": "……",
  "experiences": [
    {"company": "A 公司", "position": "产品实习生", "period": "2025-06 ~ 2025-09", "desc": "……"}
  ],
  "projects": [
    {"name": "B 项目", "role": "负责人", "period": "2025-03 ~ 2025-06", "desc": "……"}
  ],
  "skills": ["Axure", "SQL", "Python"],
  "awards": ["校级一等奖学金"]
}
```

### 第 2 步：设匹配条件

点「匹配条件」，配置 `backend/match_rules.json`：

```json
{
  "keywords": ["产品经理", "本科"],
  "cities": ["北京", "上海", "杭州", "苏州", "南京", "广州", "深圳"]
}
```

两种口径：

- **严格**：关键词**全部**命中，且城市命中
- **宽松**：关键词**任一**命中，且城市命中

关键词会在「岗位标题 + 卡面其它文字」上匹配，但看板会区分「标题里就有」与「只在其它文字里出现」，
避免误导。

### 第 3 步：扫描投递链接

点「扫描投递链接」。后端按 3 并发打开各岗位详情页，采集表单结构并缓存。
扫完可看到每个岗位的「可填字段数 / 未识别字段」。

### 第 4 步：一键投递

点「一键投递」。**单窗口严格串行**（刻意如此，避免多窗口抢焦点导致点错），逐岗位：

1. 打开详情页，必要时自动登录（验证码由手机短信转发过来）
2. 采集全部可填控件
3. 按别名表打分匹配，逐项写入
4. 上传简历 PDF
5. **停在提交前，窗口保持打开**

每条岗位的最终状态会写入 `backend/state.json`：`pending / 投递中 / 待人工提交 / 已投递 / 需人工验证`。

### 第 5 步：人工核对并提交

看板会列出「待人工提交」的岗位，点「进入窗口」把对应浏览器窗口置前，核对无误后**你自己点提交**，
再回看板点「确认已投递」。

---

## 5. 短信验证码（Android 配套 App）

很多招聘站登录需要短信验证码。本项目提供一个 Android App，把手机收到的验证码转发到同局域网 PC。

### 5.1 安装包

```
android/dist/resumebot-sms-forwarder-v2-poll.apk
```

```bash
adb install -r android/dist/resumebot-sms-forwarder-v2-poll.apk
```

或把 APK 传到手机后手动安装（需允许「安装未知来源应用」）。

### 5.2 三步授权

1. **① 授予短信 + 通知权限** —— `READ_SMS` 是轮询收件箱的前提，必须允许
2. **② 加入电池优化白名单** —— 国产 ROM 还需打开「自启动」「后台运行」
3. **③ 保存并启动轮询** —— 填后端地址 `http://<PC_IP>:8000/sms`（PC 的局域网 IP），
   轮询间隔建议 3~10 秒

> PC 与手机必须在同一局域网；Windows 防火墙需放行 8000 端口入站。

### 5.3 验证链路

App 内提供三个自检工具：

- **发送测试短信**：直接 POST 一条带随机验证码的消息，单独验证「手机 → PC」
- **补发收件箱最近 5 条**：把最近短信重新入队
- **立即冲刷待发队列**：手动触发一次发送

PC 端排查入口：`GET /api/access`（手机有没有连上来）、`GET /api/sms/history`（历史上报内容）。

### 5.4 自行构建 APK

见 [`android/README.md`](android/README.md)，含 Gradle 离线构建、前台服务类型选择、
断网队列等实现细节。

---

## 6. 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `RESUME_PORT` | `8000` | 后端端口 |
| `RESUME_XLSX` | `<项目>/岗位总表.xlsx` | 岗位表路径 |
| `RESUME_PROFILE` | `backend/profile.json` | 个人信息库 |
| `RESUME_BROWSER_CHANNEL` | `chrome` | Playwright 浏览器通道（`chrome` / `edge` / `chromium`） |
| `RESUME_CDP_PORT` | `9222` | 复用已登录浏览器时的调试端口 |
| `RESUME_CDP_UDD` | `backend/browser_profiles/_edge_cdp` | CDP 实例的资料目录 |
| `RESUME_APPLY_STUCK_GRACE` | 见源码 | 单岗位卡死判定宽限（秒） |

可选：`llm_client.py` 提供一个「简历 PDF 文本 → 结构化字段」的辅助接口，
需自行配置 `RESUME_LLM_BASE_URL` / `RESUME_LLM_API_KEY`。
**表单填写本身完全不用 LLM**，是确定性匹配。

---

## 7. 配套 Skill：职位表格式转换

`skills/job-sheet-adapter/` 是一个可安装到 AI agent 的 skill，解决「每个招聘网站的导出表
列名都不一样」的问题。

安装到 WorkBuddy：

```bash
# Windows
xcopy /E /I skills\job-sheet-adapter %USERPROFILE%\.workbuddy\skills\job-sheet-adapter
# macOS / Linux
cp -r skills/job-sheet-adapter ~/.workbuddy/skills/
```

之后把任意格式的职位 Excel 丢给 agent，说「把这个表转成 resume-bot 能用的格式」即可。
它会：

1. 自动识别列名别名（「公司全称 / 企业名称 / 单位」→ `公司`）
2. 清洗 URL、去除重复行、剔除非岗位行
3. 按关键词与城市筛选，输出 `strict` / `loose` 两档
4. 生成标准 `岗位总表.xlsx`，直接可被 `candidates.py` 读取

---

## 8. 常见问题

**Q：启动报 `FileNotFoundError: 岗位总表.xlsx`**
把表放到仓库根目录的 `岗位总表.xlsx`，或设 `RESUME_XLSX` 指向实际路径。

**Q：投递时没有弹出浏览器**
确认本机装了 Chrome；或设 `RESUME_BROWSER_CHANNEL=edge`。
Linux 服务器需额外装 Xvfb。

**Q：填了一半窗口自己关了**
正常情况下不会。窗口在「填写异常」或「等待人工提交」时会刻意保持打开。
若仍出现，查看看板该岗位的 note 字段，里面会写具体异常。

**Q：某个字段总是填错/填不上**
看板的「未填清单」会列出未命中字段。可在 `filler.py` 的 `FIELD_ALIASES` 里
补上该站点的字段文案，再重跑。

**Q：能不能自动点提交？**
不能，这是刻意的设计。自动提交意味着无法核对、无法撤回，一旦填错就是一次真实投递。

---

## 9. 开发与测试

回归测试全部离线（用假页面对象，不访问网络）：

```bash
cd backend
python smoke_t84_apply_entry.py      # 投递入口识别
python smoke_t83_keep_window.py      # 异常保窗
python smoke_t82_phoenix.py          # 北森组件
python smoke_t81_ud_sections.py      # 飞书经历/项目区块
python smoke_cascade_fill.py         # 级联填值与回读
```

每个脚本末尾输出 `PASS=x FAIL=y`，非 0 退出码表示有失败项。

---

## 10. 隐私

以下文件**不会**进入版本库（见 `.gitignore`）：

- `backend/profile.json` —— 你的姓名、手机、身份证、住址
- `backend/state.json` —— 投递记录
- `backend/sms*.json` —— 短信验证码历史
- `backend/browser_profiles/` —— 浏览器登录态与 Cookie
- `backend/*.log`、`android/local.properties`、`*.bak*`

所有数据只存在本机。项目不含任何遥测上报。

## License

仅供个人学习与求职使用。使用前请确认目标网站允许自动化操作。
