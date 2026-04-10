#!/usr/bin/env python3
"""构建并安装 SuperRightClick —— 基于 FIFinderSync 的 Finder 右键扩展。

相比旧的 Automator Quick Action 方案，这份实现使用 Apple 官方的
Finder Sync Extension API，菜单能同时出现在两种场景：
  1. 右键选中的文件 / 文件夹
  2. 右键 Finder 窗口的空白区域（旧方案无法覆盖）

架构：
  scripts/              独立 bash 脚本，每个对应一个菜单项
  src/ext/              Finder Sync extension Swift 源
  src/host/             空壳宿主 app Swift 源（只为承载 appex）
  build/                编译产物
  ~/Applications/SuperRightClick.app   最终安装位置

流程：
  1. 把 make_*_script() 的输出写成 scripts/*.sh
  2. 生成 Swift 源码（菜单 tuple 由 services 列表动态渲染）
  3. swiftc 编译 host + extension
  4. 生成两份 Info.plist
  5. codesign --sign -（ad-hoc，本机使用足够）
  6. 拷贝到 ~/Applications
  7. pluginkit 注册 & 启用扩展
  8. 清理旧 Automator 服务
  9. 打开 "系统设置 → 隐私与安全 → 扩展 → 访达扩展"，提示用户勾选
"""
import plistlib
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
SRC_EXT = ROOT / "src" / "ext"
SRC_HOST = ROOT / "src" / "host"
BUILD_DIR = ROOT / "build"
TEMPLATES_DIR = ROOT / "templates"
BLANK_DOCX = TEMPLATES_DIR / "blank.docx"

APP_NAME = "SuperRightClick"
BUNDLE_ID_APP = "com.eli.superrightclick"
BUNDLE_ID_EXT = "com.eli.superrightclick.FinderSync"
EXT_CLASS_NAME = "FinderSyncExt"

INSTALL_DIR = Path.home() / "Applications"
APP_PATH_INSTALLED = INSTALL_DIR / f"{APP_NAME}.app"


# ============================================================================
# Part 1: Shell 脚本生成器（从旧 install.py 原样继承）
# ============================================================================

_LOG_HEAD = r'''#!/bin/zsh
LOG="$HOME/Library/Logs/super-rightclick.log"
[ "$(/usr/bin/stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ] && /bin/mv "$LOG" "$LOG.1"
exec >>"$LOG" 2>&1
echo "=== $(date) [{tag}] argc=$# ==="
'''


def make_shell_script(ext, base, source=None):
    """新建文件类：touch 一个空文件，或 cp 一份模板文件。

    source 传模板文件的绝对路径。运行时脚本按 basename 在脚本同目录查找，
    install_app() 会把同名模板文件一并拷到 Application Scripts 目录里，
    这样源码树被移动或删除也不影响已安装的扩展。"""
    if source:
        template_name = Path(source).name
        create_cmd = f'/bin/cp "$(dirname "$0")/{template_name}" "$target"'
    else:
        create_cmd = '/usr/bin/touch "$target"'
    return _LOG_HEAD.format(tag=ext) + f'''printf 'arg: %s\\n' "$@"
for dir in "$@"; do
    name="{base}.{ext}"
    i=1
    while [ -e "$dir/$name" ]; do
        name="{base} $i.{ext}"
        i=$((i+1))
    done
    target="$dir/$name"
    if {create_cmd}; then
        echo "OK: $target"
        /usr/bin/osascript -e "tell application \\"Finder\\" to update (POSIX file \\"$dir\\" as alias)"
    else
        echo "FAIL: $target"
        /usr/bin/osascript -e "display notification \\"创建失败: $target\\" with title \\"新建 {ext} 文件\\""
    fi
done
'''


def make_dated_file_script(ext, source=None):
    """新建以当日日期命名的文件（默认空文件，可选模板）。"""
    if source:
        create_cmd = f'/bin/cp "{source}" "$target"'
    else:
        create_cmd = '/usr/bin/touch "$target"'
    return _LOG_HEAD.format(tag=f"dated-{ext}") + f'''for dir in "$@"; do
    base="$(date +%Y-%m-%d)"
    name="${{base}}.{ext}"
    i=1
    while [ -e "$dir/$name" ]; do
        name="${{base}} ${{i}}.{ext}"
        i=$((i+1))
    done
    target="$dir/$name"
    if {create_cmd}; then
        echo "OK: $target"
        /usr/bin/osascript -e "tell application \\"Finder\\" to update (POSIX file \\"$dir\\" as alias)"
    else
        echo "FAIL: $target"
    fi
done
'''


def make_open_ghostty_script():
    return _LOG_HEAD.format(tag="ghostty") + r'''for dir in "$@"; do
    /usr/bin/open -a Ghostty "$dir" && echo "OK: $dir" || echo "FAIL: $dir"
done
'''


def make_open_vscode_script():
    return _LOG_HEAD.format(tag="vscode") + r'''for dir in "$@"; do
    /usr/bin/open -a "Visual Studio Code" "$dir" && echo "OK: $dir" || echo "FAIL: $dir"
done
'''


def make_copy_path_script():
    return _LOG_HEAD.format(tag="copy-path") + r'''tmp=$(/usr/bin/mktemp /tmp/sr_clip.XXXXXX)
printf '%s' "$1" > "$tmp"
shift
for p in "$@"; do printf '\n%s' "$p" >> "$tmp"; done
/usr/bin/osascript -e "set the clipboard to (read (POSIX file \"$tmp\") as «class utf8»)"
/bin/rm -f "$tmp"
/usr/bin/osascript -e "display notification \"已复制路径\" with title \"复制路径\""
echo "OK: path(s) copied"
'''


def make_cut_items_script():
    return _LOG_HEAD.format(tag="cut-items") + r'''emulate -L zsh
setopt local_options no_nomatch

state_dir="$HOME/Library/Application Support/SuperRightClick"
state_file="$state_dir/cut-items.bin"
tmp_file=$(/usr/bin/mktemp /tmp/sr-cut.XXXXXX) || exit 1

if ! /bin/mkdir -p "$state_dir"; then
    /bin/rm -f "$tmp_file"
    /usr/bin/osascript -e "display notification \"无法创建状态目录\" with title \"剪切\""
    exit 1
fi

typeset -a selected

for raw_path in "$@"; do
    [ -e "$raw_path" ] || continue
    path="${raw_path:A}"

    duplicate=0
    nested_under_existing=0
    typeset -a next_selected
    next_selected=()

    for existing in "${selected[@]}"; do
        if [ "$path" = "$existing" ]; then
            duplicate=1
            next_selected+=("$existing")
            continue
        fi

        if [ -d "$existing" ] && [ "${path#"$existing"/}" != "$path" ]; then
            nested_under_existing=1
            next_selected+=("$existing")
            continue
        fi

        if [ -d "$path" ] && [ "${existing#"$path"/}" != "$existing" ]; then
            echo "DROP nested child: $existing (covered by $path)"
            continue
        fi

        next_selected+=("$existing")
    done

    selected=("${next_selected[@]}")

    if [ "$duplicate" -eq 1 ]; then
        echo "SKIP duplicate: $path"
        continue
    fi
    if [ "$nested_under_existing" -eq 1 ]; then
        echo "SKIP nested child: $path"
        continue
    fi

    selected+=("$path")
    echo "CUT: $path"
done

count="${#selected[@]}"

if [ "$count" -eq 0 ]; then
    /bin/rm -f "$tmp_file"
    /usr/bin/osascript -e "display notification \"请先选中文件或文件夹\" with title \"剪切\""
    exit 0
fi

for path in "${selected[@]}"; do
    printf '%s\0' "$path" >> "$tmp_file"
done

/bin/mv "$tmp_file" "$state_file"
/usr/bin/osascript -e "display notification \"已暂存 $count 项 | 前往目标文件夹后点击粘贴\" with title \"剪切\""
echo "OK: cut items stored count=$count"
'''


def make_paste_cut_items_script():
    return _LOG_HEAD.format(tag="paste-cut-items") + r'''emulate -L zsh
setopt local_options no_nomatch

state_dir="$HOME/Library/Application Support/SuperRightClick"
state_file="$state_dir/cut-items.bin"

if [ "$#" -ne 1 ] || [ ! -d "$1" ]; then
    /usr/bin/osascript -e "display notification \"请在目标文件夹空白处或文件夹本身使用\" with title \"粘贴\""
    exit 0
fi

dest="$1"
if [ ! -s "$state_file" ]; then
    /usr/bin/osascript -e "display notification \"当前没有已剪切的项目\" with title \"粘贴\""
    exit 0
fi

# TTL: 状态超过 24h 视为过期，清空 + 通知，避免误粘贴很久以前剪切的内容
state_age=$(( $(/bin/date +%s) - $(/usr/bin/stat -f%m "$state_file" 2>/dev/null || echo 0) ))
if [ "$state_age" -gt 86400 ]; then
    /bin/rm -f "$state_file"
    /usr/bin/osascript -e "display notification \"已剪切内容超过 24 小时，已自动清空\" with title \"粘贴\""
    exit 0
fi

dest="${dest:A}"
tmp_keep=$(/usr/bin/mktemp /tmp/sr-cut-keep.XXXXXX) || exit 1
moved=0
kept=0
missing=0
same_dir_kept=0
recursive_kept=0
failed_kept=0

while IFS= read -r -d '' src; do
    if [ ! -e "$src" ]; then
        missing=$((missing+1))
        echo "MISSING: $src"
        continue
    fi

    src="${src:A}"
    src_parent="${src:h}"
    src_name="${src:t}"
    stem="${src_name:r}"
    ext="${src_name:e}"

    if [ "$src_parent" = "$dest" ]; then
        printf '%s\0' "$src" >> "$tmp_keep"
        kept=$((kept+1))
        same_dir_kept=$((same_dir_kept+1))
        echo "KEEP same-dir: $src"
        continue
    fi

    if [ -d "$src" ]; then
        if [ "$dest" = "$src" ] || [ "${dest#"$src"/}" != "$dest" ]; then
            printf '%s\0' "$src" >> "$tmp_keep"
            kept=$((kept+1))
            recursive_kept=$((recursive_kept+1))
            echo "KEEP recursive-dir: $src"
            continue
        fi
        suffix=""
    elif [ "$stem" = "$src_name" ]; then
        suffix=""
    else
        suffix=".$ext"
    fi

    candidate="$dest/$src_name"
    i=1
    while [ -e "$candidate" ]; do
        if [ -d "$src" ] || [ "$suffix" = "" ]; then
            candidate="$dest/$src_name $i"
        else
            candidate="$dest/$stem $i$suffix"
        fi
        i=$((i+1))
    done

    if /bin/mv "$src" "$candidate"; then
        moved=$((moved+1))
        echo "MOVE: $src -> $candidate"
    else
        printf '%s\0' "$src" >> "$tmp_keep"
        kept=$((kept+1))
        failed_kept=$((failed_kept+1))
        echo "FAIL move: $src -> $candidate"
    fi
done < "$state_file"

if [ "$kept" -gt 0 ]; then
    /bin/mv "$tmp_keep" "$state_file"
else
    /bin/rm -f "$tmp_keep" "$state_file"
fi

msg="已粘贴 $moved 项"
if [ "$missing" -gt 0 ]; then
    msg="$msg | 丢失 $missing 项"
fi
if [ "$kept" -gt 0 ]; then
    msg="$msg | 保留 $kept 项待重试"
fi
/usr/bin/osascript -e "display notification \"$msg\" with title \"粘贴\""
echo "DONE: moved=$moved missing=$missing kept=$kept same_dir_kept=$same_dir_kept recursive_kept=$recursive_kept failed_kept=$failed_kept dest=$dest"
'''


def make_gen_subtitles_script():
    """用 whisper-cpp 给视频/音频生成 .srt 字幕。

    运行时依赖（NSUserUnixTask 在沙箱外跑，所以可以直接调系统命令）：
      - /opt/homebrew/bin/whisper-cli （brew install whisper-cpp）
      - /opt/homebrew/bin/ffmpeg
      - ~/whisper-models/ggml-medium.bin

    流程：ffmpeg 抽 16kHz mono pcm_s16le → whisper-cli -l zh -osrt → 删临时 wav。
    字幕与源视频同目录同名，已存在则跳过；批量选中会按顺序跑。
    长任务：NSUserUnixTask 启动后 Finder 立即返回，脚本在独立 helper 里异步跑。
    """
    return _LOG_HEAD.format(tag="gen-subtitles") + r'''emulate -L zsh
setopt local_options no_nomatch

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

MODEL="$HOME/whisper-models/ggml-medium.bin"
WHISPER_LANG="zh"

notify() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"生成 字幕\""
}

fmt_time() {
    local s=$1
    if [ "$s" -le 0 ]; then
        printf "<1s"
    elif [ "$s" -ge 3600 ]; then
        printf "%dh%dm" $((s/3600)) $(( (s%3600)/60 ))
    elif [ "$s" -ge 60 ]; then
        printf "%dm%ds" $((s/60)) $((s%60))
    else
        printf "%ds" "$s"
    fi
}

if ! command -v whisper-cli >/dev/null 2>&1; then
    notify "未找到 whisper-cli，请先 brew install whisper-cpp"
    exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    notify "未找到 ffmpeg，请先 brew install ffmpeg"
    exit 1
fi
if [ ! -f "$MODEL" ]; then
    notify "未找到模型: $MODEL"
    exit 1
fi

if [ "$#" -eq 0 ]; then
    notify "请先选中视频文件"
    exit 0
fi

# 预扫描：筛出真正要处理的文件 + 累计总时长做 ETA
typeset -a todo
total_secs=0
pre_skipped=0
for src in "$@"; do
    if [ ! -f "$src" ]; then
        echo "SKIP not a file: $src"
        pre_skipped=$((pre_skipped+1))
        continue
    fi
    if [ -e "${src%.*}.srt" ]; then
        echo "SKIP exists: ${src%.*}.srt"
        pre_skipped=$((pre_skipped+1))
        continue
    fi
    todo+=("$src")
    if command -v ffprobe >/dev/null 2>&1; then
        dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$src" 2>/dev/null)
        dur_int=${dur%.*}
        [ -z "$dur_int" ] && dur_int=0
        total_secs=$((total_secs + dur_int))
    fi
done

if [ "${#todo[@]}" -eq 0 ]; then
    notify "没有要处理的文件（已跳过 $pre_skipped 个）"
    exit 0
fi

# 实测 whisper-cpp medium 在 Apple Silicon (Metal) 上约 13x 实时，
# 每个文件再加 2s 的 ffmpeg 抽音 + whisper 启动开销。
# 3 个点（113s/942s/1288s）实测误差 < 5s。
eta=$(( total_secs / 13 + ${#todo[@]} * 2 ))

if [ "$total_secs" -gt 0 ]; then
    notify "开始生成 ${#todo[@]} 个字幕，预计 $(fmt_time $eta)"
else
    notify "开始生成 ${#todo[@]} 个字幕，后台运行中…"
fi

start_ts=$(/bin/date +%s)

ok=0
fail=0
skipped=$pre_skipped

for src in "${todo[@]}"; do
    stem="${src%.*}"
    srt="$stem.srt"

    tmp_base=$(/usr/bin/mktemp -t sr-whisper) || { fail=$((fail+1)); continue; }
    tmp_wav="${tmp_base}.wav"

    echo "--- ffmpeg: $src"
    if ffmpeg -y -i "$src" -ar 16000 -ac 1 -c:a pcm_s16le "$tmp_wav" -loglevel error; then
        echo "--- whisper-cli: $src"
        if whisper-cli -m "$MODEL" -f "$tmp_wav" -l "$WHISPER_LANG" -osrt -of "$stem"; then
            ok=$((ok+1))
            echo "OK: $srt"
            /usr/bin/osascript -e "tell application \"Finder\" to update (POSIX file \"${src:h}\" as alias)" 2>/dev/null
        else
            fail=$((fail+1))
            echo "FAIL whisper: $src"
        fi
    else
        fail=$((fail+1))
        echo "FAIL ffmpeg: $src"
    fi

    /bin/rm -f "$tmp_base" "$tmp_wav"
done

end_ts=$(/bin/date +%s)
elapsed=$((end_ts - start_ts))

msg="字幕完成 | 成功 $ok | 用时 $(fmt_time $elapsed)"
[ "$fail" -gt 0 ] && msg="$msg | 失败 $fail"
[ "$skipped" -gt 0 ] && msg="$msg | 跳过 $skipped"
notify "$msg"
echo "DONE: ok=$ok fail=$fail skipped=$skipped elapsed=${elapsed}s"
'''


# ============================================================================
# Part 2: 空白 docx 模板
# ============================================================================

_CONTENT_TYPES = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''

_RELS = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

_DOCUMENT_XML = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p/></w:body>
</w:document>'''


def ensure_blank_docx():
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    if BLANK_DOCX.exists():
        return BLANK_DOCX
    with zipfile.ZipFile(BLANK_DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", _DOCUMENT_XML)
    return BLANK_DOCX


# ============================================================================
# Part 3: 服务定义（菜单文案 → 脚本文件名 → 脚本内容）
# ============================================================================

def service_defs(docx_path):
    # (菜单文案, 脚本文件名, 脚本内容, SF Symbol 名, 允许空目标)
    # allows_empty=True 表示 Finder 既没选中项也没 targetedURL 时也要派发脚本，
    # 让脚本自己决定怎么提示/退出。只有 cut_items 需要——它的"请先选文件"
    # 通知由脚本发出。
    return [
        ("生成 字幕",          "gen_subtitles.sh", make_gen_subtitles_script(),                               "", False),
        ("新建 文本文件",      "new_txt.sh",      make_shell_script("txt",  "未命名"),                       "", False),
        ("新建 Markdown 文件", "new_md.sh",       make_dated_file_script("md"),                              "", False),
        ("新建 Word 文档",     "new_docx.sh",     make_shell_script("docx", "未命名", source=str(docx_path)), "", False),
        ("用 Ghostty 打开",    "open_ghostty.sh", make_open_ghostty_script(),                                "", False),
        ("用 VS Code 打开",    "open_vscode.sh",  make_open_vscode_script(),                                 "", False),
        ("复制路径",           "copy_path.sh",    make_copy_path_script(),                                   "", False),
        ("剪切",               "cut_items.sh",    make_cut_items_script(),                                   "", True),
        ("粘贴",               "paste_cut_items.sh", make_paste_cut_items_script(),                           "", False),
    ]


# ============================================================================
# Part 4: 写脚本文件
# ============================================================================

def write_scripts(services):
    if SCRIPTS_DIR.exists():
        shutil.rmtree(SCRIPTS_DIR)
    SCRIPTS_DIR.mkdir(parents=True)
    for _, filename, content, _, _ in services:
        path = SCRIPTS_DIR / filename
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    print(f"✓ 写入 {len(services)} 个 shell 脚本到 {SCRIPTS_DIR}")


# ============================================================================
# Part 5: 写 Swift 源码
# ============================================================================

def write_swift_sources(services):
    for d in (SRC_EXT, SRC_HOST):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    # 菜单项 tuple 列表（Swift 字面量）：(标题, 脚本文件名, SF Symbol 名, 允许空目标)
    menu_lines = ",\n        ".join(
        f'("{title}", "{filename}", "{symbol}", {"true" if allows_empty else "false"})'
        for title, filename, _, symbol, allows_empty in services
    )

    ext_swift = f'''import Cocoa
import FinderSync

@objc({EXT_CLASS_NAME})
class {EXT_CLASS_NAME}: FIFinderSync {{

    private let services: [(String, String, String, Bool)] = [
        {menu_lines}
    ]

    override init() {{
        super.init()
        // 监视根目录 → 菜单在任意文件夹可用
        FIFinderSyncController.default().directoryURLs = [URL(fileURLWithPath: "/")]
    }}

    override func menu(for menuKind: FIMenuKind) -> NSMenu {{
        let menu = NSMenu(title: "")
        let submenu = NSMenu(title: "扩展功能")

        // 跨 XPC 时 NSImage 的 isTemplate 会丢，Finder 拿到图片只会原样画，
        // 所以不能依赖 template 自动着色。自己检测当前主题，把 SF Symbol
        // 用对应颜色（深色→白，浅色→黑）渲染成静态位图再塞进菜单项。
        // menu(for:) 每次右键都会重跑，所以切主题也会即时生效。
        let appearance = NSApp.effectiveAppearance
        let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
        let tint: NSColor = isDark ? .white : .black

        for (idx, (title, _, symbol, _)) in services.enumerated() {{
            let item = NSMenuItem(
                title: title,
                action: #selector(runScript(_:)),
                keyEquivalent: ""
            )
            item.tag = idx
            if !symbol.isEmpty, let img = tintedSymbol(symbol, color: tint) {{
                item.image = img
            }}
            submenu.addItem(item)
        }}

        let parent = NSMenuItem(title: "扩展功能", action: nil, keyEquivalent: "")
        if let img = tintedSymbol("sparkles", color: tint) {{
            parent.image = img
        }}
        parent.submenu = submenu
        menu.addItem(parent)
        return menu
    }}

    private func tintedSymbol(_ name: String, color: NSColor) -> NSImage? {{
        guard let sym = NSImage(systemSymbolName: name, accessibilityDescription: nil) else {{
            return nil
        }}
        let size = NSSize(width: 16, height: 16)
        return NSImage(size: size, flipped: false) {{ rect in
            sym.draw(in: rect)
            color.set()
            rect.fill(using: .sourceAtop)
            return true
        }}
    }}

    private func debugLog(_ msg: String) {{
        NSLog("[SuperRightClick] \\(msg)")
        let logPath = ("~/Library/Logs/super-rightclick-ext.log" as NSString)
            .expandingTildeInPath
        let line = "[\\(Date())] \\(msg)\\n"
        if let data = line.data(using: .utf8) {{
            if let fh = FileHandle(forWritingAtPath: logPath) {{
                fh.seekToEndOfFile()
                fh.write(data)
                fh.closeFile()
            }} else {{
                try? data.write(to: URL(fileURLWithPath: logPath))
            }}
        }}
    }}

    @objc func runScript(_ sender: NSMenuItem) {{
        debugLog("runScript fired: \\(sender.title) tag=\\(sender.tag)")
        guard sender.tag >= 0 && sender.tag < services.count else {{
            debugLog("tag out of range")
            return
        }}
        let service = services[sender.tag]  // (title, filename, symbol, allowsEmpty)
        let filename = service.1
        let allowsEmpty = service.3

        let controller = FIFinderSyncController.default()
        let selected = controller.selectedItemURLs() ?? []

        var targets: [String] = []
        if !selected.isEmpty {{
            targets = selected.map {{ $0.path }}
        }} else if !allowsEmpty, let target = controller.targetedURL() {{
            targets = [target.path]
        }}
        debugLog("targets: \\(targets)")

        guard !targets.isEmpty || allowsEmpty else {{
            debugLog("no target")
            return
        }}

        do {{
            let scriptsURL = try FileManager.default.url(
                for: .applicationScriptsDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            let scriptURL = scriptsURL.appendingPathComponent(filename)
            debugLog("scriptURL: \\(scriptURL.path)")
            let task = try NSUserUnixTask(url: scriptURL)
            task.execute(withArguments: targets) {{ [weak self] error in
                if let error = error {{
                    self?.debugLog("script error: \\(error)")
                }} else {{
                    self?.debugLog("script ok")
                }}
            }}
        }} catch {{
            debugLog("run failed: \\(error)")
        }}
    }}
}}
'''
    (SRC_EXT / f"{EXT_CLASS_NAME}.swift").write_text(ext_swift, encoding="utf-8")

    # 扩展二进制的 stub main —— 运行时不会被执行（链接器 -e 会把入口改成 _NSExtensionMain）
    (SRC_EXT / "main.swift").write_text(
        "import Foundation\n"
        "// Unreachable. Real entry point is NSExtensionMain (set via linker -e).\n"
        "exit(0)\n",
        encoding="utf-8",
    )

    # 宿主 app —— 只是为了装载 appex；启动即退出
    (SRC_HOST / "main.swift").write_text(
        "import Cocoa\n"
        "// Host exists only to contain the FinderSync extension bundle.\n"
        "exit(0)\n",
        encoding="utf-8",
    )

    print("✓ Swift 源码已生成")


# ============================================================================
# Part 6: 编译 + 打包 + 签名
# ============================================================================

def build_app():
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    app = BUILD_DIR / f"{APP_NAME}.app"
    contents = app / "Contents"
    plugins = contents / "PlugIns"
    appex = plugins / "FinderSync.appex"
    (contents / "MacOS").mkdir(parents=True)
    (appex / "Contents" / "MacOS").mkdir(parents=True)

    # 1) 编译 host
    subprocess.run([
        "swiftc",
        "-o", str(contents / "MacOS" / APP_NAME),
        str(SRC_HOST / "main.swift"),
    ], check=True)

    # 2) 编译 extension —— 关键是链接器入口改成 _NSExtensionMain
    #    必须显式 -module-name，否则 swiftc 默认用输出名 "FinderSync"，
    #    与 Apple 的 FinderSync framework 同名，导致 import FinderSync 被忽略。
    subprocess.run([
        "swiftc",
        "-module-name", "SuperRightClickExt",
        "-o", str(appex / "Contents" / "MacOS" / "FinderSync"),
        "-framework", "FinderSync",
        "-framework", "AppKit",
        "-framework", "Foundation",
        "-Xlinker", "-e",
        "-Xlinker", "_NSExtensionMain",
        str(SRC_EXT / f"{EXT_CLASS_NAME}.swift"),
        str(SRC_EXT / "main.swift"),
    ], check=True)

    # 3) Info.plist
    host_info = {
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID_APP,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,
    }
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump(host_info, f)

    ext_info = {
        "CFBundleExecutable": "FinderSync",
        "CFBundleIdentifier": BUNDLE_ID_EXT,
        "CFBundleName": "FinderSync",
        "CFBundleDisplayName": "SuperRightClick",
        "CFBundlePackageType": "XPC!",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleSupportedPlatforms": ["MacOSX"],
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,
        "NSPrincipalClass": "NSApplication",
        "NSExtension": {
            "NSExtensionAttributes": {},
            "NSExtensionPointIdentifier": "com.apple.FinderSync",
            "NSExtensionPrincipalClass": EXT_CLASS_NAME,
        },
    }
    with open(appex / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(ext_info, f)

    # 4) 生成 entitlements —— Finder Sync extension 必须开启 sandbox
    #    否则 pkd 会记录 "plug-ins must be sandboxed" 并拒绝注册。
    ext_entitlements = {
        "com.apple.security.app-sandbox": True,
    }
    host_entitlements = {
        "com.apple.security.app-sandbox": True,
    }
    ext_ent_path = BUILD_DIR / "ext.entitlements"
    host_ent_path = BUILD_DIR / "host.entitlements"
    with open(ext_ent_path, "wb") as f:
        plistlib.dump(ext_entitlements, f)
    with open(host_ent_path, "wb") as f:
        plistlib.dump(host_entitlements, f)

    # 5) 签名：ad-hoc（"-"）+ entitlements。先签 appex 再签外层 app。
    subprocess.run([
        "codesign", "--force", "--sign", "-",
        "--entitlements", str(ext_ent_path),
        str(appex),
    ], check=True)
    subprocess.run([
        "codesign", "--force", "--sign", "-",
        "--entitlements", str(host_ent_path),
        str(app),
    ], check=True)
    print(f"✓ 已构建并签名: {app}")
    return app


# ============================================================================
# Part 7: 安装到 ~/Applications 并向 pluginkit 注册
# ============================================================================

def install_app(built_app):
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if APP_PATH_INSTALLED.exists():
        shutil.rmtree(APP_PATH_INSTALLED)
    shutil.copytree(built_app, APP_PATH_INSTALLED)
    print(f"✓ 已安装到: {APP_PATH_INSTALLED}")

    # 把 shell 脚本放到 Application Scripts 目录 —— NSUserUnixTask 只从这里加载
    app_scripts_dir = (
        Path.home() / "Library" / "Application Scripts" / BUNDLE_ID_EXT
    )
    if app_scripts_dir.exists():
        shutil.rmtree(app_scripts_dir)
    app_scripts_dir.mkdir(parents=True)
    for src in SCRIPTS_DIR.iterdir():
        dst = app_scripts_dir / src.name
        shutil.copy2(src, dst)
        dst.chmod(0o755)

    # 模板文件（如 blank.docx）也要随脚本一起放到 Application Scripts 目录，
    # 脚本里用 $(dirname "$0")/<模板名> 引用，避免依赖源码树位置。
    for tpl in TEMPLATES_DIR.iterdir():
        if tpl.is_file():
            shutil.copy2(tpl, app_scripts_dir / tpl.name)
    print(f"✓ 已安装脚本与模板到: {app_scripts_dir}")

    # 让 Launch Services 感知到新的 app / appex
    lsreg = (
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/"
        "Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
    )
    subprocess.run([lsreg, "-f", str(APP_PATH_INSTALLED)], check=False)

    appex_path = APP_PATH_INSTALLED / "Contents" / "PlugIns" / "FinderSync.appex"
    subprocess.run(["pluginkit", "-a", str(appex_path)], check=False)
    subprocess.run(["pluginkit", "-e", "use", "-i", BUNDLE_ID_EXT], check=False)
    print("✓ 已向 pluginkit 注册并启用扩展")


def remove_legacy_automator_services():
    services_dir = Path.home() / "Library" / "Services"
    removed = 0
    for p in services_dir.glob("▸*.workflow"):
        shutil.rmtree(p)
        removed += 1
    if removed:
        print(f"✓ 已移除 {removed} 个旧 Automator 服务 bundle")


# ============================================================================
# Main
# ============================================================================

def main():
    docx = ensure_blank_docx()
    services = service_defs(docx)
    write_scripts(services)
    write_swift_sources(services)
    built = build_app()
    install_app(built)
    remove_legacy_automator_services()

    # 让 Finder 重新读扩展
    subprocess.run(["killall", "Finder"], capture_output=True)

    print()
    print("=" * 60)
    print("完成。首次使用需要去系统设置里启用扩展（一次性）：")
    print("  系统设置 → 隐私与安全 → 扩展 → 访达扩展")
    print("  在列表里勾选 SuperRightClick")
    print("=" * 60)
    # 尝试自动打开扩展面板
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.ExtensionsPreferences"],
        capture_output=True,
    )


if __name__ == "__main__":
    main()
