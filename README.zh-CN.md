# Cuddler for ComfyUI

Cuddler Video、Image、Speech、Music 四个节点根据目录展示可选模型和参数。
组织的实际模型权限和每次花费上限均由 Cuddler 在接受请求前校验。

[English](README.md)

用你的 [Cuddler Platform](https://platform.cuddler.ai) API key，在 ComfyUI 工作流里生成视频、图片、语音和音乐。
四个节点使用公开模型目录。运行前节点上会显示价格，每次运行都有花费上限，由 Cuddler 服务端强制执行。

## 需要准备

- ComfyUI 0.14.0 或更新的版本。
- 一个 Cuddler Platform API key。在 <https://platform.cuddler.ai/console/keys> 创建，在
  <https://platform.cuddler.ai/console/billing> 充值。

## 安装

在 ComfyUI-Manager 里搜索 **Cuddler**，或者运行 `comfy node install cuddler`，然后重启 ComfyUI。
插件不需要额外安装 Python 包。

如果还搜不到 Cuddler，说明公开发行尚未可用。

## 视频

选择模型和用法，填写提示词，把 VIDEO 输出接到 ComfyUI 的 **Save Video**。按模型能力提供文生、首帧、首尾帧、混合参考、图片参考、视频参考、编辑、续写、动作控制。
参考接口接受原生 IMAGE、VIDEO、AUDIO；每接入一项会自动增加接口，直到目录上限。图片不接受批量；视频保留宿主裁剪范围；上传素材移除工作流元数据。
支持的模型开启 `return_last_frame` 后，第二个 IMAGE 输出可接 Save Image；未开启时该输出为空。

参考视频上限 200 MiB（动作控制 100 MiB），参考音频为 2–15 秒、最多 50 MiB，另须满足所选模型的尺寸、时长及数量限制。
费用依赖参考视频时，标签显示起步价；运行时按实际上传视频时长精确估价，超过花费上限则不创建任务。

模型及参数来自公开目录，但模型能否被当前组织使用，仍由带身份的估价和创建接口检查。保存工作流并固定 seed，才能在重启后恢复同一个操作。

## 图片与音频

- **Cuddler Image**：选择模型、用法和版本。参考图和编辑模式可从 Load Image 接入图片，数量不超过模型上限；IMAGE 输出接 Save Image。
- **Cuddler Speech**：选择模型、声音并填写文本；对话模型可选第二位说话人，也可用 `dialogue_script` 输入最多 50 句、10 种声音。每行写为 `1: 你好`、`2: 你好呀`，编号对应声音选项；使用脚本时留空主文本并关闭 `second_speaker`。AUDIO 输出接 Save Audio。语音估价需要将输入文本发送给 Cuddler，按文本和声音计算费用。
- **Cuddler Music**：选择模型、填写提示词，AUDIO 输出接 Save Audio。

图片解码为 RGB；音频保留原采样率及单声道或双声道。图片下载上限为 30 MiB、3200 万像素；
音频为 50 MiB、10 分钟。无效或超限输出会停止节点，再次运行仍恢复原任务，不新建收费替代任务。
不支持上游 seed 的模型仍使用 seed 标识本地操作。

## 填写 API key

打开 **设置 → Cuddler**，粘贴 key，点 **保存**。页面上会显示 key 的最后 4 位和可用余额。

部署在服务器上时，改用环境变量 `CUDDLER_API_KEY`。它的优先级高于设置页里保存的 key。

设置页和价格标签只响应从 `localhost`、IP 地址或局域网名称（例如 `my-pc.local`）打开的页面，这样其他网站就改不了你的 key。如果通过域名或隧道访问 ComfyUI，请把该域名加入环境变量 `CUDDLER_ALLOWED_HOSTS`（多个用逗号分隔，`.example.com` 表示它下面的所有域名），然后重启 ComfyUI。

## 费用

- 运行前，每个节点都会显示价格。价格来自 Cuddler 的估价接口，查价不收费。
- 每个节点都有单次花费上限（默认 $2）。可能花费超过上限的任务，Cuddler 不会创建，实际扣费也不会超过上限。
- 用同样的参数和同样的 seed 再次运行时，结果保留期内会直接取回上次结果，不会重复扣费。想要新的结果，改一下 seed。结果过期后再次运行会创建新任务，并重新扣费。
- 视频、语音、音乐任务在排队时停止运行，任务会被取消，不扣费。图片任务和已经开始生成的任务无法取消，会正常完成并扣费，再次运行这个节点即可取回结果。

## 隐私与安全

- key 保存在 `ComfyUI/user/__cuddler/credentials.json`。ComfyUI 不会通过网页接口提供 `__` 开头文件夹里的文件；在 macOS 和 Linux 上，这个文件只有你的账户能读。
- key 不会写进工作流、图片或视频的元数据、ComfyUI 的设置或日志。
- 平台 API 请求在发送 key 或请求数据前校验目标为 `https://api.cuddler.ai`，系统代理仍可使用。签名媒体上传保留原存储请求头，但拒绝认证、Cookie 和代理认证请求头；下载不会附带平台 key。
- `ComfyUI/user/__cuddler/state.sqlite3` 保存运行指纹和文件哈希、幂等键、任务和上传 ID、花费上限及不透明的凭据绑定，不保存提示词和素材。结果未知的提交保留原幂等键及重放所需信息，未决记录保留至结果查明；终态任务在 8 天后清理，已就绪上传在 29 天后清理。
- 更换 key 后，只有新 key 成功查询已知任务，才会恢复该任务。尚未拿到任务 ID 的提交需要原 key；缺少绑定、无法读取旧任务或数据库损坏时，会停止运行，不自动新建收费任务。请保留原记录，在平台控制台核对或联系客服；**不要通过删除数据库来重试**。
- 请将 `state.initialized` 标记与任务库一起保留，避免丢失的任务库被误当成全新安装。
- 发给 Cuddler 的每个任务都带有元数据 `client: comfyui-cuddler/<版本号>`，用来统计从 ComfyUI 发起的任务数量；每个请求的 User-Agent 里写有插件和 Python 的版本号。除此之外，不会发送任何关于你或你电脑的信息。上传的参考素材会带上它的文件名。
- ComfyUI 本身没有登录功能。能打开你 ComfyUI 页面的人都能用你的 key 花钱，所以不要把 ComfyUI 开放到不可信的网络上。单次上限只限制每个任务，不限制累计花费；账户每日上限仅在已经配置时生效，不要假设存在默认的总额保护。
- 如果 ComfyUI 运行在另一台机器上，在设置页填写的 key 会经过网络传过去。这种情况建议改用 `CUDDLER_API_KEY`。

## 开发

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .
npm test
```

发版前运行 `python scripts/export_catalog_snapshot.py`，更新插件自带的模型目录。

实际验收按[视频验收操作步骤](docs/acceptance/video-paid.md)执行。预检只读取目录、余额和估价；
真实生成与结算需要先登记预算批准并另留证据，宿主 CI 不能替代该验收。
网络边界补丁还需核对[兼容回归清单](docs/acceptance/network-boundaries.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。
