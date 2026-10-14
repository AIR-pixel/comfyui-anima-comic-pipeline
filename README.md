# comfyui-anima-comic-pipeline

一条本地 **漫画产出流水线**：用 Anima 模型在 ComfyUI 上出图，脚本由 AI 写。

把 **任务书 → 任意 AI 写分镜 → ComfyUI 自动出图** 串起来，纯 Python 标准库，**零依赖**。

- **①任务书**：页面内置任务书生成器，填好故事与人物 → 下载一份自包含的 `.md` 任务书
- **→** 把任务书交给**任意 AI**（ChatGPT / Claude / 本地模型都行），按任务书里的输出协议拿回分镜 md
- **②生产**：把分镜 md 拖进页面 → 自动填进 Anima 文生图工作流 → 逐格出图，结果按章节落盘

![任务书生成器](screenshots/1_任务书生成器.png)

## 你需要有什么

| 依赖 | 说明 |
| --- | --- |
| **ComfyUI** | 必需的出图引擎。已在跑就直接复用；没跑则由流水线自动拉起、用完自动关 |
| **Anima 文生图模型** | UNET + 文本编码器(CLIP) + VAE，可选 LoRA。在页面「模型配置」里下拉选择（实时读取你 ComfyUI 的模型目录） |
| **Comfyroll 自定义节点** | 工作流模板用到它的 `CR Prompt Text` / `JoinStringMulti` 节点 |
| **Python 3.8+** | 只用标准库，无需 pip 安装任何东西 |

> **平台**：目前在 **Windows** 上验证（自动拉起 / 自动关闭依赖 Windows 的
> `create_no_window` 与 `taskkill`）。非 Windows 下若已经手动开好 ComfyUI，
> 也能复用出图，只是自动拉起不生效。

> 没装 Comfyroll 也不用慌：`workflow_template.json` 是一份普通 ComfyUI API 工作流，
> 换成你自己的文生图工作流即可（保证最后有一个 `SaveImage` 节点）。

## 快速开始

```bash
python anima_pipeline.py
```

浏览器打开 **http://127.0.0.1:8790** 。

常用参数：

```bash
python anima_pipeline.py --port 9000                       # 换端口
python anima_pipeline.py --comfy-url http://192.168.1.10:8188   # 复用远程 ComfyUI
python anima_pipeline.py --comfy-python <python.exe> --comfy-main <ComfyUI/main.py>
python anima_pipeline.py --keep-comfy                      # 出图后不关引擎（连续出好几章时用）
```

`--comfy-python / --comfy-main` 只在**自动拉起**时用得到。默认值是 `ComfyUI-aki-v3`
的布局；其他安装请手动先启动 ComfyUI（流水线会直接复用），或用这两个参数指定路径。
`Ctrl+C` 退出。

## 用法

**① 任务书**
在生成器里填故事、人物、生图参数（草稿自动保存），点「生成并下载任务书 (.md)」。
把这份 md 整份发给任意 AI，按「多轮对话脚本」逐章要 JSON，每章存成一份 md。

**② 生产**
切到「② ComfyUI 生产」→ 把分镜 md 拖进上传区 →（可选）调模型 / LoRA →
点「开始生产」。每格依次进队列，缩略图可点开看大图，完成后给出结果文件夹路径。

分镜 md 的格式见 [`示例_第1章.md`](示例_第1章.md)：只要含一个带 `panels` 数组的
JSON 代码块就行，其余文字随意。

![生产页](screenshots/2_生产页_配置与上传.png)

出图后：

```
结果/第N章/
├─ 01_id1.png  02_id2.png ...   每格一张
└─ manifest.json                每格的完整提示词、seed、所用模型配置
```

![生产结果](screenshots/3_生产结果.png)

## ComfyUI 引擎自管理

不用手动管 ComfyUI。点「开始生产」时：

1. 探测 `127.0.0.1:8188`（严格校验返回的是 ComfyUI 的 JSON，避免被系统代理的 502 假信号骗到）
2. **已在跑**（不管谁起的）→ 直接复用，退出流水线时也不关它
3. **没在跑** → 后台静默拉起，日志写到 `logs/comfy_engine.log`，等就绪约 40–70 秒
4. **生产完成** → 自动关掉流水线拉起的实例（`--keep-comfy` 可让它常驻）

## 目录结构

```
comfyui-anima-comic-pipeline/
├─ anima_pipeline.py           主程序（纯标准库）
├─ static/index.html           网页界面（两个标签页）
├─ static/taskbook/index.html  任务书生成器（独立页面）
├─ workflow_template.json      Anima 文生图工作流模板（可替换成你自己的）
├─ engine_env/README.md        自动拉起用的环境变量说明
├─ screenshots/                README 配图
└─ 示例_第1章.md               分镜 md 格式示例
```

## 常见问题

**装了 ComfyTV，自动拉起的 ComfyUI 秒退？**
自动拉起的子进程会带 `COMFYTV_TESTING=1`，跳过 ComfyTV 在 import 阶段的启动备份
（否则无头进程会卡在删除确认上直接退出）。这个变量只影响自动拉起；你手动启动
ComfyUI 时没有它，ComfyTV 功能完整。详见 [`engine_env/README.md`](engine_env/README.md)。

**想换模型 / 改尺寸、步数、cfg？**
模型在页面上选。尺寸、步数、cfg 缺省值写在 md 的每格 JSON 里（不写则用模板默认
`920×1536 / 30 步 / cfg 5`）。想改默认值就编辑 `workflow_template.json`。

## 致谢

任务书协议仿自 **Butako11 / AnimaComicStudio** 的 `ComfyUI-Anima-Story` 节点组。
Anima 模型权重为非商业许可，使用请遵循各模型作者的条款。
