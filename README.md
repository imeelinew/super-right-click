# SuperRightClick —— Finder 右键扩展

> 给接手的 agent：读完本文即可修改 / 新增 / 删除功能，并理解之前踩过的所有坑。
> 本文档描述的是 **FIFinderSync 版本**（基于 Apple 官方 Finder Sync Extension API）。
> 早期的 Automator Quick Action 版本已废弃。

## 是什么

一套基于 **FIFinderSync extension** 的 Finder 右键扩展。相比旧的 Automator 方案，优势是菜单同时出现在两种场景：

1. 右键选中的文件 / 文件夹
2. **右键 Finder 窗口空白区域**（旧版做不到）

当前右键菜单顶层只有一个 `扩展功能` 二级菜单。

我们自己实现的 10 个动作都挂在里面，按固定顺序连续排列。外层父菜单保留图标，二级菜单项不显示图标。

| 菜单位置 | 菜单文案 | 脚本 | SF Symbol | 功能 |
|---|---|---|---|---|
| 扩展功能 | 生成字幕 | `gen_subtitles.sh` | `""` | 选中视频/音频 → 用 whisper-cpp medium 生成同目录同名 `.srt`（zh、支持批量、自动规整长字幕）；不改原文件 |
| 扩展功能 | 新建文本文件 | `new_txt.sh` | `""` | 新建 `未命名.txt`（冲突自动编号） |
| 扩展功能 | 新建 Markdown 文件 | `new_md.sh` | `""` | 新建 `YYYY-MM-DD.md`（当日日期命名） |
| 扩展功能 | 新建 Word 文档 | `new_docx.sh` | `""` | 新建 `未命名.docx`（基于内置最小合法 docx 模板） |
| 扩展功能 | 用 Ghostty 打开 | `open_ghostty.sh` | `""` | `open -a Ghostty "$dir"` |
| 扩展功能 | 用 VS Code 打开 | `open_vscode.sh` | `""` | `open -a "Visual Studio Code" "$dir"` |
| 扩展功能 | 提交并推送当前仓库 | `git_commit_push.sh` | `""` | 仅当当前目录本身直接含 `.git` 且就是仓库根目录时执行；要求当前分支已设置上游，然后执行 `git add -A && git commit -m "YYYY-MM-DD HH:mm:ss" && git push`。**注意**：`git add -A` 会把所有未跟踪文件（含 `.env`、密钥、大二进制等）一并加入提交；且 commit message 只是时间戳，历史不可读 —— 仅适合临时仓库/草稿目录，重要仓库请走正常 git 流程 |
| 扩展功能 | 复制路径 | `copy_path.sh` | `""` | 把绝对路径写入剪贴板 + 通知（UTF-8 安全） |
| 扩展功能 | 剪切 | `cut_items.sh` | `""` | 自定义“剪切”所选文件/文件夹，暂存到扩展自己的状态文件 |
| 扩展功能 | 粘贴 | `paste_cut_items.sh` | `""` | 把之前“剪切”的文件/文件夹移动到当前目录，冲突自动编号 |

## 三层架构

整套东西分三层，运行时只跑下面两层，Python 只是构建器：

| 层 | 语言 | 运行时机 | 作用 |
|---|---|---|---|
| 构建器 | Python (`install.py`) | 只在 `python3 install.py` 时 | 生成 Swift/脚本/plist → `swiftc` 编译 → `codesign` 签名 → 拷贝到 `~/Applications` → `pluginkit` 注册 |
| Finder 插件 | Swift (`FinderSyncExt`) | Finder 每次右键触发 | 实现 `FIFinderSync`；`menu(for:)` 返回顶层 `扩展功能` 子菜单；`runScript(_:)` 用 `NSUserUnixTask` 启动对应脚本 |
| 菜单动作 | bash (`scripts/*.sh`) | 用户点菜单时 | 真正干活的代码（创建文件、剪切/粘贴移动、复制路径、开 Ghostty 等） |

## 文件布局

```
~/Dev/super-rightclick/
├── install.py                  ← 唯一源码（约 1200 行，所有逻辑都在这里）
├── README.md                   ← 本文件
├── templates/blank.docx        ← install.py 首次运行时由 ensure_blank_docx() 生成
├── scripts/                    ← install.py 生成的 bash 脚本（每个菜单项一个）
├── src/ext/FinderSyncExt.swift ← install.py 生成的 Swift 源（根据 services 列表动态拼）
├── src/ext/main.swift          ← stub（入口被链接器改成 _NSExtensionMain）
├── src/host/main.swift         ← 壳 app stub
└── build/                      ← swiftc 编译产物
```

安装后的文件：

```
~/Applications/SuperRightClick.app                            ← 壳 app + appex
~/Library/Application Scripts/com.eli.superrightclick.FinderSync/*.sh
                              ↑ NSUserUnixTask 只从这个目录加载脚本，必须拷贝过去
~/Library/Logs/super-rightclick.log      ← 脚本运行日志（bash 写入）
~/Library/Logs/super-rightclick-ext.log  ← Swift extension 调试日志（debugLog 写入）
```

## install.py 做的事（按顺序）

1. `ensure_blank_docx()` —— 用 `zipfile` 生成最小合法 `.docx`（3 个 XML 打包）
2. `service_defs(docx)` —— 返回 `[(菜单文案, 脚本文件名, 脚本内容, SF Symbol)]` 列表
3. `write_scripts()` —— 写 `scripts/*.sh`
4. `write_swift_sources()` —— 根据 services 列表动态拼 `FinderSyncExt.swift`，菜单项是 `[(String, String, String)]` 字面量
5. `build_app()` ——
   - `swiftc` 编译 host 壳
   - `swiftc -module-name SuperRightClickExt -Xlinker -e -Xlinker _NSExtensionMain` 编译 extension（**见坑 1 和坑 2**）
   - `plistlib.dump` 写两份 Info.plist（host + appex，**见坑 3**）
   - 写 entitlements plist（**见坑 4**）
   - `codesign --force --sign - --entitlements ...` 临时签名
6. `install_app()` ——
   - 拷贝 app bundle 到 `~/Applications/`
   - 拷贝 `scripts/*` 到 `~/Library/Application Scripts/<ext-bundle-id>/`（**见坑 5**）
   - `lsregister -f` 让 Launch Services 感知
   - `pluginkit -a` 注册，`pluginkit -e use` 启用
7. `remove_legacy_automator_services()` —— 清理旧 Automator `~/Library/Services/▸*.workflow`

## 已知的坑（加功能前必读）

### 坑 1：swiftc 默认 module name 和 Apple 的 FinderSync framework 同名

不加 `-module-name SuperRightClickExt` 时，swiftc 会默认用输出二进制名 `FinderSync` 作为 module name，**和 Apple 的 `FinderSync` framework 冲突**。结果：`import FinderSync` 被静默忽略，`FIFinderSync` / `FIMenuKind` / `FIFinderSyncController` 全都找不到。一定要显式指定 module name。

### 坑 2：Finder Sync extension 入口必须是 `NSExtensionMain`

直接 `swiftc` 编译出来的二进制入口是普通 `main`（来自 stub `main.swift`），Finder 加载后啥都不干。必须用 `-Xlinker -e -Xlinker _NSExtensionMain` 告诉链接器把入口改成 Apple 的 `NSExtensionMain`。stub `main.swift` 只是为了让编译器有东西可链接，里面写 `exit(0)` 就行，永远不会被执行。

### 坑 3：Finder Sync extension 必须沙箱化

这是折腾最久的坑。症状：`pluginkit -m -p com.apple.FinderSync` 看不到扩展，`pluginkit -a` 返回 0 但没效果。

在 `log show --predicate 'process == "pkd"' --last 5m` 里可以看到决定性的一行：

```
pkd: rejecting; Ignoring mis-configured plugin at [...]: plug-ins must be sandboxed
```

修法：生成 entitlements plist，`codesign --entitlements` 签名时带上：

```python
{"com.apple.security.app-sandbox": True}
```

并且 Info.plist 必须带全下列字段，少一个都可能让 pkd 拒绝：

```python
{
    "CFBundleSupportedPlatforms": ["MacOSX"],
    "NSPrincipalClass": "NSApplication",
    "LSMinimumSystemVersion": "11.0",
    "LSUIElement": True,
    "CFBundleInfoDictionaryVersion": "6.0",
    # + 标准 CFBundle* 字段
    # + NSExtension 字典（NSExtensionPointIdentifier = "com.apple.FinderSync"）
}
```

### 坑 4：沙箱内不能 `Process()` 启动任意二进制

扩展既然沙箱了，就不能直接 `Process(); task.launchPath = "/bin/zsh"`——会被 sandbox 拒绝，菜单点击完全无反应。

**唯一正路：`NSUserUnixTask`**。这是 Apple 留给沙箱 app 的逃生通道，特点是被它启动的子进程**运行在沙箱外**，可以任意读写用户文件系统（这正是我们需要的——脚本要在用户右键的任意文件夹里创建文件）。

```swift
let scriptsURL = try FileManager.default.url(
    for: .applicationScriptsDirectory,
    in: .userDomainMask,
    appropriateFor: nil, create: true
)
let task = try NSUserUnixTask(url: scriptsURL.appendingPathComponent(filename))
task.execute(withArguments: targets) { error in ... }
```

### 坑 5：NSUserUnixTask 只从 `~/Library/Application Scripts/<ext-bundle-id>/` 加载

这是配套坑 4 的限制。脚本**必须**放在这个目录，而且要用扩展自己的 bundle id（不是 host app 的）。`install_app()` 里专门有一步 `shutil.copy2` 把 `scripts/*` 拷过去。

### 坑 6：NSMenuItem 的 `target` 和 `representedObject` 跨 XPC 丢失

最初写的：

```swift
item.target = self
item.representedObject = filename
```

结果菜单显示 OK，点击**完全没反应**——`runScript` 根本不会被调用，日志什么都没有。因为 Finder 把菜单通过 XPC 序列化到自己的进程里展示，点击后再反序列化回 extension 进程时，`target` 对象引用和 `representedObject`（任意 NSObject）都不保证还原。

修法：

1. **不要设 `item.target`**，让 responder chain 自己找到 extension 实例派发
2. **不要用 `representedObject`**，用 `item.tag = idx`（Int 是基本类型，肯定能穿 XPC），`runScript` 里用 tag 查 services 数组

### 坑 7：Launch Services 需要一次 `open` + `lsregister -f`

`install.py` 里有 `subprocess.run([lsreg, "-f", ...])`。如果缺这步，即使签名和 plist 都对，`pkd` 日志里会看到 `-10814` "Unable to find this application extension record in the Launch Services database"。

### 坑 8：bash 脚本里 `pbcopy` 从 Finder 触发不可靠（继承自旧版）

Finder 触发时脚本退出瞬间管道子进程被回收，`pbcopy` 来不及写完。从 CLI 跑没问题，从 Finder 跑就跪。

**正解**：用 AppleScript 的 `set the clipboard to`。

### 坑 9：AppleScript `system attribute` 按 MacRoman 读环境变量（继承自旧版）

最初用 `export SR_PAYLOAD=xxx` + `osascript -e 'set the clipboard to (system attribute "SR_PAYLOAD")'` 规避引号嵌套。ASCII 路径没问题，**UTF-8 中文会乱码**。

**正解**：写临时文件 + `read (POSIX file "...") as «class utf8»`，全链路显式 UTF-8。见 `make_copy_path_script()`。

### 坑 10：macOS 通知文本的 `\n` 会被截断（继承自旧版）

`display notification` 的文本里 `\n` 只会显示第一行。多行内容要拼成一行，用 ` | ` 之类分隔。

### 坑 11：NSImage 的 `isTemplate` 跨 XPC 丢失，深色模式下 SF Symbol 是黑的

给 `NSMenuItem.image` 赋一个 SF Symbol 并设 `img.isTemplate = true`，在 ext 进程内看是正确的 template image，AppKit 本该按文字色自动着色。但菜单通过 XPC 序列化到 Finder 进程时 **`isTemplate` 标记不保留**，Finder 拿到的是普通黑色 symbol，结果浅色下正常、深色下几乎看不见。

**正解**：在 `menu(for:)` 里自己检测当前主题，把 symbol 用目标颜色（深色→白、浅色→黑）**渲染成静态位图**再塞进菜单项。`menu(for:)` 每次右键都会重跑，所以切主题时下次右键就会刷新。

```swift
let isDark = NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
let tint: NSColor = isDark ? .white : .black
// tintedSymbol(name, color: tint) 内部：
//   lockFocus → draw symbol → color.set + rect.fill(using: .sourceAtop) → unlockFocus
// sourceAtop 只在有像素的地方上色，背景透明
```

见 `FinderSyncExt.swift` 里的 `tintedSymbol(_:color:)`。不要试图走 `isTemplate = true` 这条路，XPC 那关过不去。

## 如何添加新功能

**所有菜单共享同一套构建流程，加新项只改 `service_defs()` 一处**。

每项是 5 元组：`(菜单文案, 脚本文件名, 脚本内容, SF Symbol 名, allows_empty)`。
- SF Symbol 名可以去 [SF Symbols app](https://developer.apple.com/sf-symbols/) 里挑，传空串 `""` 表示不要图标。
- `allows_empty=True` 表示当 Finder 没选中任何文件、也拿不到窗口 `targetedURL` 时仍然派发脚本（让脚本自己决定怎么兜底）；目前只有「剪切」用到。绝大多数菜单写 `False` 即可。

### 场景 A：新增"新建 XXX 文件"类菜单

复用 `make_shell_script(ext, base, source=None)` 或 `make_dated_file_script(ext, source=None)`（日期命名）：

```python
# service_defs() 里加一行
("新建 HTML 文件", "new_html.sh", make_shell_script("html", "未命名"), "chevron.left.forwardslash.chevron.right", False),
("新建今日笔记", "new_note.sh", make_dated_file_script("md"), "calendar", False),
```

需要模板文件时，两种做法：
1. **小文件代码内生成**：仿 `ensure_blank_docx()`，生成到 `templates/xxx`，`source=str(path)` 传入
2. **现成文件**：扔进 `templates/`，`source` 指过去即可

### 场景 B：新增"操作型"菜单（不创建文件）

新写一个 `make_xxx_script()` helper。模板：

```python
def make_xxx_script():
    return _LOG_HEAD.format(tag="xxx") + r'''for dir in "$@"; do
    # 真正的逻辑写这里
    # 要写剪贴板：临时文件 + read as «class utf8»，不要用 pbcopy 或 system attribute
    # 要发通知：/usr/bin/osascript -e 'display notification "..." with title "..."'
done
'''
```

然后在 `service_defs()` 里加 `("菜单文案", "xxx.sh", make_xxx_script(), "sf.symbol.name", False)`。

### 场景 C：新增调用其它 app 的菜单

`make_open_ghostty_script()` 就是范本，本质上是 `open -a AppName "$dir"`。记住脚本是通过 `NSUserUnixTask` 起的，运行在沙箱外，可以任意 `open`。

### 最后一步：生效

```bash
cd ~/Dev/super-rightclick && python3 install.py
# install.py 最后会自己 killall Finder 以便立刻加载新菜单；
# 若正在拖拽/重命名等，可手动重跑 killall Finder。
```

## 如何修改已有功能

- **改菜单文案**：改 `service_defs()` 里元组第一项，重跑 `install.py`
- **改菜单图标**：改 `service_defs()` 里元组第四项（SF Symbol 名），重跑
- **改 shell 行为**：改对应的 `make_xxx_script()` 函数体
- **改输入类型**（比如让菜单也出现在选中单个文件时）：当前 `FinderSyncExt.swift` 里 `menu(for:)` 不区分 `menuKind`，所有场景返回同一个菜单。如果想区分，用 `if menuKind == .contextualMenuForItems` / `.contextualMenuForContainer` / `.contextualMenuForSidebar` 分别返回不同菜单
- **改监视范围**：`FIFinderSyncController.default().directoryURLs = [...]`，当前设为 `[URL(fileURLWithPath: "/")]`，即整个磁盘任意文件夹都生效

## 调试

1. **点击无反应**：先看 `~/Library/Logs/super-rightclick-ext.log`。有 `runScript fired` 说明 Swift 端 OK，问题在脚本；没有说明 `menu(for:)` 或菜单点击派发挂了，重读坑 6
2. **脚本日志**：`~/Library/Logs/super-rightclick.log`，每次触发追加一段 `=== 时间 [tag] argc=N ===`
3. **系统层面**：`log show --predicate 'process == "pkd" OR process == "FinderSync"' --last 5m`。pkd 的错误通常明确告诉你为啥被拒（沙箱、签名、plist 字段等）
4. **pluginkit 状态**：`pluginkit -mAvvv -p com.apple.FinderSync | grep -A5 super`。`+` 前缀 = 已启用，`?` = 未启用（去系统设置里勾），没这一条 = 根本没注册
5. **扩展进程**：`pgrep -fl FinderSync.appex`。平时没进程很正常（macOS 会回收），右键一次后应该能看到

### 问题 → 排查速查表

| 现象 | 可能原因 | 对策 |
|---|---|---|
| 菜单不显示 | 未启用 | 系统设置 → 登录项与扩展 → 文件提供程序/访达扩展，勾选 SuperRightClick |
| 菜单显示但点击无反应，ext 日志空 | 坑 6（target/representedObject） | 用 `item.tag`，不设 target |
| ext 日志有 `runScript fired`，脚本日志空 | 坑 4/5（NSUserUnixTask 路径） | 确认脚本在 `~/Library/Application Scripts/<ext-bundle-id>/` |
| pluginkit 看不到扩展 | 坑 3（未沙箱化） | entitlements 里加 `com.apple.security.app-sandbox` |
| `pkd` 日志 `plug-ins must be sandboxed` | 同上 | 同上 |
| `pkd` 日志 `-10814` | 坑 7（LS 没注册） | `lsregister -f ~/Applications/SuperRightClick.app` |
| `import FinderSync` 没效果，编译报 FIFinderSync undefined | 坑 1（module name 冲突） | `swiftc -module-name SuperRightClickExt ...` |
| 剪贴板英文 OK 中文乱码 | 坑 9 | 见 `make_copy_path_script()` |
| 通知换行被吞 | 坑 10 | 用 ` \| ` 拼一行 |
| 图标浅色正常深色变黑看不清 | 坑 11（isTemplate 跨 XPC 丢失） | 自己在 `menu(for:)` 里按 `effectiveAppearance` 渲染 tint |

## 如何完全卸载

```bash
# 1. 停用并取消注册扩展
pluginkit -e ignore -i com.eli.superrightclick.FinderSync
pluginkit -r ~/Applications/SuperRightClick.app/Contents/PlugIns/FinderSync.appex 2>/dev/null

# 2. 删除安装的 app 和脚本
rm -rf ~/Applications/SuperRightClick.app
rm -rf "$HOME/Library/Application Scripts/com.eli.superrightclick.FinderSync"

# 3. 刷新 Launch Services + 重启 Finder
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -kill -r -domain local -domain user
killall Finder

# 4. 删除日志
rm -f ~/Library/Logs/super-rightclick.log ~/Library/Logs/super-rightclick-ext.log

# 5. (可选) 删除项目源码
rm -rf ~/Dev/super-rightclick
```

再跑一次 `python3 ~/Dev/super-rightclick/install.py` 即可复原（前提是源码还在）。

## 性能

- **内存**：extension 进程常驻约 28 MB RSS。有时会有两个进程（Finder 一个，「打开」对话框等其它调用方一个）
- **CPU**：常驻 0%。FIFinderSync 是事件驱动的，仅在右键 / 点击瞬间唤醒
- **空闲回收**：一段时间不用，pkd 会把进程回收，RAM 归零，下次右键时冷启动（50-100 ms，感知不到）

## 扩展方向（留给后续）

- 按菜单场景区分菜单项（文件夹 vs 文件 vs 侧边栏），见上面"如何修改已有功能"
- 给菜单项加图标：`item.image = NSImage(...)`，图片放 appex 的 `Resources/` 里，Info.plist 里 `CFBundleIconFile`
- 支持二级菜单：`item.submenu = NSMenu(...)`
- 把 `service_defs()` 抽成 YAML/JSON 配置文件，`install.py` 纯执行。目前菜单项少，不值得

## Code review 修订记录（2026-04-22）

这一轮没有加新功能，只做防御性加固和文档同步。按 commit 顺序记录改动和思路，便于回退定位。

### 1. README 文档漂移修复（commit `1793c25`）

三处描述跟实现对不上：

| 位置 | 旧表述 | 实际 | 影响 |
|---|---|---|---|
| 文件布局 | `install.py` "约 400 行" | 现 1200+ 行 | 误导阅读预期 |
| "如何添加新功能" | 4 元组 `(title, filename, content, symbol)` | 5 元组，含 `allows_empty` | 按文档加功能会编译失败 |
| "最后一步：生效" | "install.py 本身不 killall Finder" | `main()` 末尾有 `killall Finder` | 与实际行为矛盾 |

`allows_empty` 本来是新加的字段（目前只有「剪切」= `True`），控制"选中为空且拿不到 `targetedURL`"时是否仍派发脚本——让脚本自己决定怎么提示，见 `FinderSyncExt.runScript`。

### 2. 生成的 Swift 字面量用 `json.dumps` 转义（commit `b894049`）

`write_swift_sources()` 之前用 f-string 拼 Swift 字符串字面量：

```python
f'("{title}", "{filename}", "{symbol}", ...)'
```

如果 `service_defs()` 里哪天写了个带 `"` 或 `\` 的标题，swiftc 会直接报语法错。换成 `json.dumps(s, ensure_ascii=False)` —— JSON 字符串的转义规则是 Swift 的超集（`\"`、`\\`、`\n`、`\t`、`\uXXXX` 全兼容），等价且安全。生成结果对现有纯中文/ASCII 标题二进制一致，无行为变化。

### 3. `copy_path.sh` 零参数守护 + 移除空 `NSExtensionAttributes`（commit `a4ee568`）

**前者**：脚本原本先 `printf '%s' "$1" > "$tmp"` 再 `shift`，在 `$# == 0` 时 zsh 的 `shift` 会报错、剪贴板还会被写空。改成开头检查 `$#`，无参数直接发通知退出；有参数时用 `first=1` 标志位处理首行，无需 `shift`。

**后者**：`ext_info["NSExtension"]` 里的 `"NSExtensionAttributes": {}` 是空字典，Finder Sync 扩展 pkd 不读任何键——删掉之后 `pluginkit -mAvvv` 仍显示扩展处于 `+` 已启用状态，验证确实无用。

### 4. Swift `Service` struct 替代裸 tuple + `debugLog` 加固（commit `f184604`）

**Struct 化**：原本 `[(String, String, String, Bool)]` + `service.1` / `service.3` 位置访问，加字段/换顺序就是隐形坑。换成：

```swift
private struct Service {
    let title: String
    let filename: String
    let symbol: String
    let allowsEmpty: Bool
}
```

`menu(for:)` 和 `runScript(_:)` 里全部改成命名访问。install.py 的生成代码同步更新。

**`debugLog` 线程安全 + 1 MB 轮转**：`NSUserUnixTask.execute(..., completion:)` 的 completion block 在任意线程回调，`menu(for:)`、`runScript`、completion 三处都会调 `debugLog`，原本裸 `FileHandle` 写入可能互相穿插。改法：

1. 加 `logQueue = DispatchQueue(label: "...log")` 串行队列，所有写入都 `async` 到这条队列
2. 写入前 `attributesOfItem` 检查文件大小，超过 1 MB 移到 `.log.1`，策略跟 bash 脚本的 `_LOG_HEAD` 里那段一致
3. 顺手把 `FileHandle.seekToEndOfFile()` 换成 macOS 11+ 的 `seekToEnd()` / `write(contentsOf:)` / `close()`（项目 `LSMinimumSystemVersion = 11.0`，都可用）

### 5. README 补 `提交并推送当前仓库` 的使用警告（commit `bf889b2`）

菜单里这个动作实际是 `git add -A && git commit -m "<时间戳>" && git push`，有两个隐患：
- `git add -A` 会把未跟踪文件（含 `.env`、密钥、大二进制等）一并提交——依赖 `.gitignore` 卫生
- commit message 全是时间戳，历史完全不可读

没有改脚本行为（用户原意就是临时仓库一键推），只在菜单表格里加了警告。

### 没做的

- **`install.py.automator.bak`**：`.gitignore` 已忽略，是用户本地备份，没删
- **`cut_items.sh`/`paste_cut_items.sh` 加 flock**：理论上有撕裂风险，但 Finder 菜单触发速率下实际概率很低，flock 带来的复杂度不值得
- **`gen_subtitles.sh` 的 MODEL 路径可配置化**：目前硬编码 `$HOME/whisper-models/ggml-medium.bin`，要改成环境变量/配置文件涉及新增机制，留给下轮
- **加测试**：`normalize_srt` 的分句逻辑、`ensure_blank_docx` 都是纯函数，值得加 pytest，但需要引入 CI/目录结构，留给下轮

### 验证方式

每个 commit 都走了同一套本地验证：

```bash
python3 install.py                                                         # 重编译 + 重签名 + 重注册
pluginkit -mAvvv -p com.apple.FinderSync | grep -c "+ *com.eli.superrightclick"  # 必须 = 1
for f in ~/Library/Application\ Scripts/com.eli.superrightclick.FinderSync/*.sh; do bash -n "$f"; done
```

能验证：构建/签名/pluginkit 注册/脚本语法。**不能**从命令行验证：Finder 右键菜单的真实点击行为，需要手动试。如果哪项菜单点下去没反应，按 commit hash `git revert` 就能退回对应那步之前。

