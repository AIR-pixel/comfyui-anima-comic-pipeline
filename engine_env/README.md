# engine_env —— 为什么存在这个目录

`anima_pipeline.py` 自动拉起 ComfyUI 时，会给子进程设两个环境变量（逻辑见 `_spawn_comfy()`）：

- `PYTHONPATH` = 本目录：切断对宿主进程 `PYTHONPATH` 的继承，避免 IDE / 代理 / 其他工具的路径混进 ComfyUI 的 import 搜索路径。
- `COMFYTV_TESTING=1`：见下。

本文件只记机理，目录本身保持为空（不放任何 `.py`）。

## COMFYTV_TESTING=1

如果你装了 **ComfyTV** 这个自定义节点，它的 `__init__.py` 在 import 阶段会执行
`run_startup_backup()`：把 `ComfyUI\user\comfytv` 数据目录快照到
`custom_nodes\ComfyTV\db-backup\<时间戳>\`，再按设置 `db-backup-max-count`
prune 掉最旧的快照（`shutil.rmtree`）。

当被 prune 的旧快照文件数超过 50 时，某个删除守卫会在控制台打印
`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {...}` 并要求确认，
然后才继续。交互式控制台回答一下就行；但流水线是用无头子进程拉起的
（`stdin=DEVNULL`），等不到输入会直接退出，ComfyUI 永远起不来。

`COMFYTV_TESTING=1` 是 ComfyTV 自带的测试模式开关：`__init__.py` 见到该变量即跳过
backup / db 初始化 / runners / api / storage / nodes 的加载，`comfy_entrypoint`
会抛 `RuntimeError`；ComfyUI 对 `comfy_entrypoint` 的调用带 try/except，捕获后
**只跳过 ComfyTV 这一个节点**，服务和其他所有节点不受影响。本流水线的 Anima
文生图工作流不用 ComfyTV 节点，因此零损失。

只有「流水线自动拉起」的子进程带这个变量；你自己手动启动 ComfyUI 时没有它，
ComfyTV 功能完整。

> 没装 ComfyTV：这个变量无害，什么都不会发生。
