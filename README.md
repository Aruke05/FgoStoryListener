# FGO 剧情文本监听器

FgoStoryListener v2.11.2-stage3（本地改进版）在模拟器旁显示日服 FGO 的日文台词、剧情选项和简体中文译文，并将双语记录保存在本机。

开启整关预翻译后，程序在后台按关卡 ID、阶段从 Atlas Academy 查询日服剧本，不向该服务上传游戏 LOG 或译文。用至少三条独立台词按顺序认出剧本后，可以提前准备全段有明确姓名、整句在该角色下唯一的静态页。带槽位的姓名（如 `A：松永久秀`）只取明写的名字，不根据 `A` 猜角色。新格式的候选必须等游戏 LOG 的关卡、姓名和整句吻合后才显示。动态称呼、同角色的歧义重复句、未识别指令仍走实时翻译；纯标点直接保留。后台候选默认不显示在主表。

主窗口 **阅读速度** 可查看本次游戏 LOG 到达时的就绪率、译文窗首段更新 P90 和等待原因。未在窗口测到的页面不参与延迟统计。诊断保存在应用数据目录 `reading-diagnostics/`，不上传：最多两个约 4 MB 的轮转文件，包含本地预载文字片段、关卡信息、页面指纹和计时。退出后可删除；设置环境变量 `FGO_READING_REPLAY=0` 可不保存原文片段。运行 `python tests/replay_reading_session.py` 查看最近会话；加 `--replan` 可用录到的预载输入和同版本公开剧本复核候选覆盖，不能把覆盖率当成实测就绪率。

## 开始使用

| 平台 | 模拟器准备 | 启动程序 |
| --- | --- | --- |
| Apple Silicon Mac | 在 BlueStacks Air 的高级设置中开启 ADB，实例需提供内置 `su` | `FgoStoryListener.app` |
| Windows 10/11 x64 | 在雷电模拟器 9 中开启本机 ADB 和 Root 权限，重启模拟器 | `FgoStoryListener.exe` |

1. 首次使用中文翻译前，在终端运行 `codex login` 登录 Codex CLI。
2. 在模拟器中启动日服 FGO，再打开监听器。状态显示“监听中”后进入剧情。
3. 边玩边读可点“仅保留译文窗”。点“查看 LOG”回看对话，点“解释这句”查看上下文。主窗口支持按关卡筛选、复制和导出 TXT。

剧情选项出现时会按原编号翻译。在“设置”中可分别选择实时翻译与整关预翻译的模型。翻译失败时点“重试未翻译”。

[主窗口截图](docs/screenshots/main-window.png) · [双语 LOG 截图](docs/screenshots/bilingual-log.png)

## 本地数据

| macOS | Windows |
| --- | --- |
| `~/Library/Application Support/FgoStoryListener/` | `%LOCALAPPDATA%\FgoStoryListener\` |

`history.db` 保存双语历史，`config.json` 保存设置，`knowledge/` 保存更新后的术语库。

## 从源码构建

安装支持 Tk 的 Python 3.11 或更新版本，以及 Git LFS。克隆仓库后，在仓库根目录执行对应命令。

**macOS**

```bash
git lfs pull
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
./build-macos.sh
```

**Windows PowerShell**

```powershell
git lfs pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

macOS 产物在 `dist-macos/`，Windows 产物在 `dist-v2.11.2-stage3/`。运行 `python -m unittest discover -s tests -v` 执行单测；`python tests/smoke_stage_reader_ui.py` 验证实际 Tk 译文窗。历史第 7/8 话的时序回放可运行 `python tests/replay_chapter.py 9416400810 --online`：需留有对应旧记录；用保留下来的锚点重建输入，真实调用已配置模型，在临时数据库按原 LOG 时间放行译文，不操作模拟器。不加 `--online` 只验候选覆盖和缓存取用。
