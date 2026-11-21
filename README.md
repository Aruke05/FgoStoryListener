# FGO 剧情文本监听器

FgoStoryListener v2.11.2-stage1（本地改进版）在模拟器旁显示日服 FGO 的日文台词、剧情选项和简体中文译文，并将双语记录保存在本机。

开启整关预翻译后，程序在后台按关卡 ID、阶段从 Atlas Academy 查询日服剧本，不上传游戏 LOG 或译文。只预翻能和本机文字顺序唯一对应、有明确姓名的完整页；动态称呼、选项、含未知指令的页面、离线或剧本不匹配的情况沿用实时翻译。已核实的前文可作为上下文，不替玩家选择变量。关闭整关预翻译也会关闭这次公开剧本查询。

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

macOS 产物在 `dist-macos/`，Windows 产物在 `dist-v2.11.2-stage1/`。使用虚拟环境中的 Python 运行 `python -m unittest discover -s tests -v` 可执行测试。手动运行 `python tests/smoke_stage_online.py` 可用真实剧本与已配置模型重放卑弥呼用例；测试使用临时数据库。
