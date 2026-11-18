# 简历机器人 · 短信转发 App（v2 · 持续轮询版）

把手机收到的**短信验证码**转发到同局域网内运行 `resume-bot` 后端的 PC
（POST JSON 到 `http://<PC_IP>:8000/sms`），供看板展示与人工确认（断点2）。

## v2 为什么改成轮询

v1 用手册式思路（`NotificationListenerService` 读短信通知）——**实测装了却从不上报**
（后端两次复测 `/sms` 的 POST 计数恒为 0，`sms_rejected.jsonl` 为 0 字节；
同一时段手机浏览器 GET `/sms` 返回 200，证明网络与后端都正常）。原因归纳为：

| v1 路径 | 为什么不可靠 |
|---|---|
| `SMS_RECEIVED` 广播 | Android 10（API 29）起只投递给**默认短信应用**，普通 App 收不到 |
| `NotificationListenerService` | 需要用户单独去「通知使用权」授权；通知可能被 ROM 限流、折叠或延迟 |

v2 改为**直接轮询收件箱** `content://sms/inbox`，只依赖 `READ_SMS` 一个运行时权限，
语义上就是「读短信」，不依赖任何系统 App 的转发行为。

## 目录结构

```
android/
├── gradle-dist/gradle-8.11.1/      # 本地解压的 Gradle（无需联网下载）
├── build.gradle / settings.gradle  # AGP 8.10.0 + Kotlin 1.9.24，仓库走阿里云镜像
├── local.properties                # sdk.dir 指向本机 Android SDK
└── app/src/main/
    ├── AndroidManifest.xml
    ├── java/com/example/resumebot/
    │   ├── SmsPollService.kt           # ★ 主路径：前台服务 + 轮询线程
    │   ├── Forwarder.kt                # 公共转发层：待发队列 / POST / 统计 / 日志
    │   ├── MainActivity.kt             # 配置页：权限、地址、启停、自检
    │   ├── BootReceiver.kt             # 开机/更新后自拉起
    │   ├── SmsNotificationListener.kt  # 第二路径（可选）：通知使用权
    │   └── SmsReceiver.kt              # 旧路径：仅 Android 9 及以下（清单里 enabled=false）
    ├── res/layout/activity_main.xml
    ├── res/values/themes.xml
    └── res/xml/notification_listener_info.xml
```

## 核心机制

### 1. 增量轮询

- **首次运行**：把 `last_sms_id` 播种为收件箱当前最大 `_id`，**只转发此后到达的新短信**，
  不会把历史短信一次性灌进后端（需要验证历史可用界面上「补发收件箱最近 5 条」）。
- **之后每轮**：`_id > last_id` 按 `_id` 升序读取 → 逐条入待发队列 → 推进 `last_id`。

### 2. 断网不丢

短信先写入 `SharedPreferences` 的**待发队列**（按短信 `_id` 去重），再冲刷后端：

- 顺序发送，**首次网络类失败即中止本轮**（判定链路断，避免 300 条 × 6 秒超时白等）；
- `HTTP 4xx` 视为「服务端明确拒收」，**单条丢弃并计数**，不阻塞队列；
- 每轮最多实际尝试 30 条，其余留到下轮；队列上限 300 条，溢出丢最旧。

因为两条来源（轮询 / 通知）共用同一队列且按 `_id` 去重，同一条短信不会被上报两次。

### 3. 前台服务类型为什么用 `specialUse`

`dataSync` 类型在 Android 15+ 有「每 6 小时强制回调 `onTimeout`、当日不可再启动」的限制，
会导致长时间停止轮询。个人短信转发工具无法归入 `dataSync`/`camera`/`media` 等标准类别，
因此声明 `specialUse` 并给出子类型说明（`personal_sms_forwarder`），无时长上限。
`startForeground` 已做降级兜底：`specialUse` 被 ROM 拒绝时退化为普通前台服务。

## 打包（已在 Windows 本机验证可离线构建）

APK 用**本地解压的 Gradle** 构建，不需要 Android Studio，也不需要联网下载 Gradle：

```powershell
$env:JAVA_HOME='<JDK17 安装目录>'
cd <本目录>
.\gradle-dist\gradle-8.11.1\bin\gradle.bat --console=plain assembleDebug
# 产出：app/build/outputs/apk/debug/app-debug.apk
```

前置条件：Android SDK（`local.properties` 里的 `sdk.dir`）、JDK 17。
Gradle 依赖缓存（`~/.gradle/caches`）里已有 androidx 全部依赖，仓库指向
`maven.aliyun.com`（本机可达）。

## 安装与授权（Android 16 实测路径）

```bat
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

打开 App，按界面上的 ①②③ 顺序操作：

1. **① 授予短信 + 通知权限** —— `READ_SMS` 是轮询的前提，必须允许。
2. **② 加入电池优化白名单** —— 防止国产 ROM 把前台服务冻结/杀掉。
   若系统设置里还有「自启动」「后台运行」开关，一并允许。
3. **③ 保存并启动轮询** —— 填后端完整地址（形如 `http://<PC_IP>:8000/sms`）
   与轮询间隔（建议 3~10 秒）。

界面上的验证工具：

- **发送测试短信**：直接 POST 一条带随机验证码的自检消息，用来单独验证「手机→PC」链路。
- **补发收件箱最近 5 条**：把最近短信重新入队上报。
- **立即冲刷待发队列**：手动触发一次队列发送。
- 主页每秒刷新「服务状态 / 最近心跳 / 统计（已上报·待发·失败·拒收）/ 运行日志」。

## 与后端的关系

- 后端 `server.py` 提供 `POST /sms`（绑定 `0.0.0.0:8000`），接受 `{phone, code, text}`，
  也接受 form 编码、chunked、纯文本正文等宽容形态。
- App 只发 `phone` / `code` / `text` / `ts` 四个 JSON 字段。
  **刻意不带 `src` 之类的短名**——后端 `SMS_FIELD_ALIASES` 把 `src` 也算作 `phone` 的别名，
  发件人为空时会被误写成发件人。
- 排查入口：`GET /api/access`（手机到底有没有连上来）、`GET /api/sms/history`（历史上报内容）、
  `GET /api/sms/rejected`（被拒台账）、`sms_rejected.jsonl`（拒收明细，0 字节＝一条都没被拒）。
