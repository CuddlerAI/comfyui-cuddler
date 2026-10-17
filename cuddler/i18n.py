"""What an error means and what to do about it, in English or Chinese.

The language follows ComfyUI's own setting (Comfy.Locale) for the default user.

A message can appear in a node's error, next to a node's price label and on the settings page, where no node is
running. Advice that only fits a run ("run the node again") is worded so that it still reads right elsewhere.
"""
import json

KEYS_URL = "https://platform.cuddler.ai/console/keys"
BILLING_URL = "https://platform.cuddler.ai/console/billing"
MAX_SETTINGS_BYTES = 4 * 1024 * 1024  # a real comfy.settings.json is a few KB; a file this big is not read

MESSAGES = {
    "key_missing": (
        f"Add your Cuddler API key in Settings → Cuddler. Create one at {KEYS_URL}.",
        f"请先在「设置 → Cuddler」里填写 Cuddler API key。可以在 {KEYS_URL} 创建。"),
    "key_invalid_format": (
        "That is not a Cuddler API key. Copy the whole key, which starts with cud_sk_live_ or cud_sk_test_, "
        "without quotes or spaces. If it comes from the CUDDLER_API_KEY environment variable, fix it there.",
        "这不是有效的 Cuddler API key。请完整复制 key（以 cud_sk_live_ 或 cud_sk_test_ 开头），不要带引号或空格。"
        "如果 key 来自环境变量 CUDDLER_API_KEY，请在环境变量里修改。"),
    "key_unreadable": (
        "The saved key could not be read. Save it again in Settings → Cuddler.",
        "读取不到已保存的 key，请在「设置 → Cuddler」里重新保存。"),
    "key_from_environment": (
        "The key comes from the CUDDLER_API_KEY environment variable. Change it there, then restart ComfyUI.",
        "当前的 key 来自环境变量 CUDDLER_API_KEY，请在环境变量里修改，然后重启 ComfyUI。"),
    "key_unwritable": (
        "The key file could not be changed. Make sure ComfyUI can write to its user folder and that no "
        "other program is using the file, then try again.",
        "无法修改 key 文件。请确认 ComfyUI 对 user 文件夹有写入权限，且没有其他程序占用该文件，然后重试。"),
    "forbidden_origin": (
        "ComfyUI only answers pages it serves under its own address, so this page cannot read or change the "
        "Cuddler settings. Open ComfyUI at http://localhost or by its IP address (with its port). If you reach "
        "it through a domain name, add that name to the CUDDLER_ALLOWED_HOSTS environment variable and "
        "restart ComfyUI.",
        "ComfyUI 只响应从它自己的地址打开的页面，所以这个页面不能读取或修改 Cuddler 设置。"
        "请用 http://localhost 或本机的 IP 地址（带端口）打开 ComfyUI；如果是通过域名访问，"
        "请把该域名加入环境变量 CUDDLER_ALLOWED_HOSTS，然后重启 ComfyUI。"),
    "test_key": (
        "A test key can only show prices. Use a live key to generate.",
        "测试 key 只能查价格，生成需要正式（live）key。"),
    "unauthorized": (
        f"The key was rejected: it is invalid or has been revoked. Create a new one at {KEYS_URL}.",
        f"key 无效或已被撤销，请到 {KEYS_URL} 换一个新 key。"),
    "insufficient_balance": (
        f"Not enough balance. Add funds at {BILLING_URL}.",
        f"余额不足，请到 {BILLING_URL} 充值。"),
    "forbidden": ("This key is not allowed to do that.", "这个 key 没有权限做这件事。"),
    "model_not_enabled": (
        "This model is not enabled for your organization.", "这个模型没有对你的组织开放。"),
    "organization_suspended": (
        "Your organization is suspended. Please contact support.", "你的组织已被暂停，请联系客服。"),
    "content_blocked": (
        "The platform blocked this prompt or media. Change it and try again.",
        "内容被平台拦截，请修改提示词或素材后再试。"),
    "rate_limited": ("Too many requests. Wait a moment and try again.", "请求太频繁，请稍后再试。"),
    "queue_full": (
        "Your queue is full. Wait for running jobs to finish.", "排队已满，请等正在生成的任务完成后再试。"),
    "daily_limit_reached": (
        "Today's spending limit has been reached. It resets at 00:00 UTC.",
        "今天的花费已经达到上限，UTC 0 点重置。"),
    # The check is on the most a job can be charged (the Platform's hold_usd), which is a little above its estimate.
    "over_cap": (
        "This run could cost up to {hold}, above this node's limit of {cap}. "
        "Raise the limit or lower the settings.",
        "这次运行最多可能花费 {hold}，高于这个节点的上限 {cap}。请调高上限，或调低相关参数。"),
    "invalid_cap": (
        "The spending limit must be between $0.01 and $1000.", "单次花费上限需要在 $0.01 到 $1000 之间。"),
    "invalid_amount": (
        "A price or amount could not be read. Update the plugin.", "无法识别价格或金额，请更新插件。"),
    "invalid_request": ("The platform did not accept these settings.", "平台不接受这组参数。"),
    "not_found": (
        "The platform could not find that job or upload; it may have expired. "
        "If you were running a node, run it again.",
        "平台上找不到这个任务或上传的文件，可能已过期。如果刚才在运行节点，请再运行一次。"),
    "not_cancelable": (
        "The job has already started and cannot be canceled. It will finish and be charged.",
        "任务已经开始生成，无法取消，会正常完成并扣费。"),
    "idempotency_conflict": (
        "The platform saw different inputs for the same request. Please report this to Cuddler.",
        "平台发现同一请求的参数前后不一致，请把这个问题反馈给 Cuddler。"),
    "network_error": (
        "Could not reach Cuddler. Check your internet connection and try again. "
        "A node that was running picks the same job back up when you run it again.",
        "连接 Cuddler 失败，请检查网络后重试。如果刚才在运行节点，再次运行会接上同一个任务。"),
    "platform_unavailable": (
        "Cuddler is temporarily unavailable. Try again shortly.", "Cuddler 暂时不可用，请稍后再试。"),
    "invalid_response": (
        "Cuddler sent a response this plugin does not understand. Update the plugin.",
        "收到了插件无法识别的响应，请更新插件。"),
    "response_too_large": ("The response was too large.", "返回的内容过大。"),
    "unsafe_url": ("Refused an unsafe link.", "拒绝了一个不安全的链接。"),
    "media_host_not_allowed": (
        "Refused a download link outside Cuddler's storage. Update the plugin.",
        "拒绝了一个不属于 Cuddler 存储的链接，请更新插件。"),
    "media_transfer_failed": ("A file transfer failed. Try again.", "文件传输失败，请重试。"),
    "media_type_not_allowed": ("This file type is not supported.", "不支持这种文件格式。"),
    "media_too_large": ("This file is larger than the {limit} limit.", "这个文件超过了 {limit} 的上限。"),
    "upload_rejected": ("The platform rejected this media: {reason}", "平台拒绝了这份素材：{reason}"),
    "upload_expired": ("The upload expired. Run the node again.", "上传已过期，请重新运行。"),
    "upload_checking": (
        "The platform is still checking this file. Run the node again in a minute: it continues with the same upload.",
        "平台还在检查这份素材。请过一会儿重新运行，会接着同一次上传继续。"),
    "job_failed": ("The job failed: {reason}", "任务失败：{reason}"),
    "job_canceled": ("The job was canceled.", "任务已取消。"),
    "output_expired": (
        "The earlier result has expired. Running the node again generates a new result and charges for it.",
        "之前的结果已过期，再运行一次会重新生成并重新计费。"),
    # Two outcomes of stopping a run: the queued job was canceled (interrupted_canceled), or it could not be
    # canceled (started already, an image job, or the cancel call failed) and keeps running (interrupted).
    "interrupted": (
        "Stopped waiting, but the job was not canceled: it keeps running on Cuddler and will be charged. "
        "Run the node again to collect the result.",
        "已停止等待，但任务没有被取消：它会在 Cuddler 上继续生成并正常计费。再次运行这个节点即可取回结果。"),
    "interrupted_canceled": (
        "Stopped. The queued job was canceled.", "已停止，排队中的任务已取消。"),
    "interrupted_before_create": (
        "Stopped before the job was sent. Nothing was charged.", "已在发送任务前停止，没有产生费用。"),
    "wait_timeout": (
        "Not finished after an hour. Run the node again to keep waiting.",
        "等了一小时还没完成。再次运行这个节点会继续等待。"),
    "state_unavailable": (
        "Cuddler could not use its local job records ({reason}). Make sure ComfyUI can write to its user "
        "folder and that no other program is using it, then run the node again.",
        "Cuddler 无法读写本地任务记录（{reason}）。请确认 ComfyUI 对 user 文件夹有写入权限，且没有其他程序占用，然后重新运行。"),
    "recovery_required": (
        "Stopped to avoid charging twice: the earlier request cannot be safely recovered with this key or "
        "these local records. Restore the original key and records, or contact Cuddler support to reconcile "
        "the earlier request before generating again. Do not delete the records to retry.",
        "为避免重复计费，已停止：当前 key 或本地记录无法安全恢复之前的请求。请恢复原 key 和记录，"
        "或联系 Cuddler 支持核对之前的请求后再生成。不要删除记录来重试。"),
    "catalog_invalid": ("The model list could not be loaded.", "模型列表加载失败。"),
}
ALIASES = {"internal_error": "platform_unavailable", "upstream_error": "platform_unavailable",
           "unavailable": "platform_unavailable"}
FALLBACK = ("Something went wrong ({code}).", "出错了（{code}）。")
REQUEST_SUFFIX = (" (request {request_id})", "（请求编号 {request_id}）")


class _Fill(dict):
    def __missing__(self, name):
        return "?"


def language_from_settings(path):
    """'zh' when ComfyUI is set to Chinese, otherwise 'en'. Never raises: a file that cannot be read means 'en'."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) > MAX_SETTINGS_BYTES:
            return "en"
        # utf-8-sig: a byte order mark (Windows editors add one) is not part of the JSON. ValueError also
        # covers bad UTF-8; RecursionError is what json.loads raises for a file nested thousands deep.
        locale = json.loads(raw.decode("utf-8-sig")).get("Comfy.Locale")
    except (OSError, ValueError, AttributeError, RecursionError):
        return "en"
    return "zh" if isinstance(locale, str) and locale.lower().startswith("zh") else "en"


def describe(error, language="en"):
    index = 1 if language == "zh" else 0
    code = ALIASES.get(error.code, error.code)
    template = MESSAGES.get(code, FALLBACK)[index]
    text = template.format_map(_Fill(error.detail, code=error.code))
    if error.request_id:
        text += REQUEST_SUFFIX[index].format(request_id=error.request_id)
    return text
