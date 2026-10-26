"""配置读取与校验。

字段语义与默认值依据见 PLAN.md §5。
构造完成后 `paths` 内的路径已解析为绝对路径。

设计取舍：默认值一律用**相对路径**（`work` / `models`），这样仓库 clone 到任何
位置都能直接跑；`config.example.yaml` 里给的是本机推荐值（绝对 D 盘路径）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

import yaml

T = TypeVar("T")

DEFAULT_CONFIG_NAME = "config.yaml"
EXAMPLE_CONFIG_NAME = "config.example.yaml"

VALID_DEVICES = ("auto", "cuda", "cpu")
VALID_COMPUTE_TYPES = ("auto", "float16", "float32", "int8", "int8_float16")

#: `transcribe.language` 的**空串**含义：交给 faster-whisper 自动检测。
#:
#: 为什么不用 `"auto"` 这个字面量：faster-whisper 只认 `None`（`language=None`
#: 才走 detect_language）。传 `"auto"` 或 `""` 会在 `transcribe()` 里被当成语言码
#: 去查表，直接抛 `ValueError: Language 'auto' is not supported`。
#: 配置层用空串（YAML 里好写、好判断），**归一成 `None` 只发生在
#: `app/core/transcriber.py` 一个地方**。
LANGUAGE_AUTO = ""

#: 语言码 → 中文名。**这张表就是唯一真源**：`SUPPORTED_LANGUAGES` 由它派生，
#: 设置界面的下拉项由 `get_config` 下发（前端不另存一份）。
#:
#: 为什么硬编码而不 `from faster_whisper.tokenizer import _LANGUAGE_CODES`：
#: `config.py` 被 `paths.py` / `cli.py` 这类极早期模块导入，在这里 import
#: faster_whisper 会把 CTranslate2 原生库拖进启动路径（而"非主线程首次原生
#: import 静默卡死"是本项目踩过的坑）。一致性由
#: `tools/rpc_smoke.py::check_language_table()` 对着已安装的包核对，
#: **漂移会被判据抓住，不靠注释保证**。
LANGUAGE_LABELS: dict[str, str] = {
    "af": "南非荷兰语",
    "am": "阿姆哈拉语",
    "ar": "阿拉伯语",
    "as": "阿萨姆语",
    "az": "阿塞拜疆语",
    "ba": "巴什基尔语",
    "be": "白俄罗斯语",
    "bg": "保加利亚语",
    "bn": "孟加拉语",
    "bo": "藏语",
    "br": "布列塔尼语",
    "bs": "波斯尼亚语",
    "ca": "加泰罗尼亚语",
    "cs": "捷克语",
    "cy": "威尔士语",
    "da": "丹麦语",
    "de": "德语",
    "el": "希腊语",
    "en": "英语",
    "es": "西班牙语",
    "et": "爱沙尼亚语",
    "eu": "巴斯克语",
    "fa": "波斯语",
    "fi": "芬兰语",
    "fo": "法罗语",
    "fr": "法语",
    "gl": "加利西亚语",
    "gu": "古吉拉特语",
    "ha": "豪萨语",
    "haw": "夏威夷语",
    "he": "希伯来语",
    "hi": "印地语",
    "hr": "克罗地亚语",
    "ht": "海地克里奥尔语",
    "hu": "匈牙利语",
    "hy": "亚美尼亚语",
    "id": "印尼语",
    "is": "冰岛语",
    "it": "意大利语",
    "ja": "日语",
    "jw": "爪哇语",
    "ka": "格鲁吉亚语",
    "kk": "哈萨克语",
    "km": "高棉语",
    "kn": "卡纳达语",
    "ko": "韩语",
    "la": "拉丁语",
    "lb": "卢森堡语",
    "ln": "林加拉语",
    "lo": "老挝语",
    "lt": "立陶宛语",
    "lv": "拉脱维亚语",
    "mg": "马达加斯加语",
    "mi": "毛利语",
    "mk": "马其顿语",
    "ml": "马拉雅拉姆语",
    "mn": "蒙古语",
    "mr": "马拉地语",
    "ms": "马来语",
    "mt": "马耳他语",
    "my": "缅甸语",
    "ne": "尼泊尔语",
    "nl": "荷兰语",
    "nn": "新挪威语",
    "no": "挪威语",
    "oc": "奥克语",
    "pa": "旁遮普语",
    "pl": "波兰语",
    "ps": "普什图语",
    "pt": "葡萄牙语",
    "ro": "罗马尼亚语",
    "ru": "俄语",
    "sa": "梵语",
    "sd": "信德语",
    "si": "僧伽罗语",
    "sk": "斯洛伐克语",
    "sl": "斯洛文尼亚语",
    "sn": "修纳语",
    "so": "索马里语",
    "sq": "阿尔巴尼亚语",
    "sr": "塞尔维亚语",
    "su": "巽他语",
    "sv": "瑞典语",
    "sw": "斯瓦希里语",
    "ta": "泰米尔语",
    "te": "泰卢固语",
    "tg": "塔吉克语",
    "th": "泰语",
    "tk": "土库曼语",
    "tl": "他加禄语",
    "tr": "土耳其语",
    "tt": "鞑靼语",
    "uk": "乌克兰语",
    "ur": "乌尔都语",
    "uz": "乌兹别克语",
    "vi": "越南语",
    "yi": "意第绪语",
    "yo": "约鲁巴语",
    "zh": "中文",
    "yue": "粤语",
}

SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(LANGUAGE_LABELS)



class ConfigError(Exception):
    """配置文件缺失或字段非法。"""


def _section(cls: type[T], data: Any) -> T:
    """从 dict 构造配置段，忽略未知键与显式 null（用 dataclass 默认值兜底）。"""
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known and v is not None})


@dataclass
class PathsConfig:
    """路径配置。

    默认值都是**相对路径**（`workspace` / `models`），由调用方锚定到 `root`
    （源码运行 = 仓库根）。打包运行时的默认值不走这里，而是
    `paths.pick_default_work_dir()` / `pick_default_model_dir()`：
    优先 `<安装目录>/workspace`、`<安装目录>/models`，安装目录不可写时回退。

    ⚠️ 目录名 2026-10-26 从 `work` 改成 `workspace`，**字段名 `work_dir` 不变** ——
    它是配置与协议里的契约，改名没有用户可见收益，只会多一处漂移风险。
    """

    work_dir: str = "workspace"
    model_dir: str = "models"
    #: 用户是否**明确确认过**工作目录。
    #:
    #: 语义与默认值都很讲究：
    #: - 默认 `True` ⇒ 老配置（yaml 里没有这个键）不会被弹引导，升级用户不被打扰；
    #: - 只有**首启自动生成**的配置才被置为 `False`（见 `paths.ensure_default_config`），
    #:   于是界面弹一次性引导，把「自动挑的 D:\Vidigest\work」摆给用户确认或改。
    #:
    #: 为什么不靠「路径是不是默认值」判断：用户可能就是想用 `%LOCALAPPDATA%\work`，
    #: 那样每次启动都会弹 —— 用一个显式的确认位才不会有这种死循环。
    work_dir_confirmed: bool = True
    # None 表示按 ffmpeg_locator 的四级顺序自动定位（PLAN.md §9.4）
    ffmpeg: str | None = None
    # None 表示按 yt_dlp_locator 的顺序自动定位（config → PATH）
    yt_dlp: str | None = None
    # Netscape 格式 cookies.txt。None 表示不带 cookie 下载（公开视频可用；
    # 抖音多数内容需要，见 PLAN.md §9.5 —— ttwid 约 24 小时过期）
    cookies_file: str | None = None

    def resolve(self, root: Path) -> PathsConfig:
        """把相对路径锚定到项目根目录。"""
        work = Path(self.work_dir).expanduser()
        model = Path(self.model_dir).expanduser()
        cookies = Path(self.cookies_file).expanduser() if self.cookies_file else None
        return PathsConfig(
            work_dir=str(work if work.is_absolute() else root / work),
            model_dir=str(model if model.is_absolute() else root / model),
            # ⚠️ 这个字段**必须**一起带过来：`resolve()` 是逐字段重建，
            # 漏一个就会在每次 `Config.from_dict` 时被静默重置成默认值，
            # 表现是「首启引导弹了、用户点确认、下次启动又弹」。
            work_dir_confirmed=self.work_dir_confirmed,
            ffmpeg=self.ffmpeg,
            yt_dlp=self.yt_dlp,
            cookies_file=(
                str(cookies if cookies.is_absolute() else root / cookies) if cookies else None
            ),
        )


@dataclass
class RuntimeConfig:
    """运行期设备与并发上限。

    `device` / `compute_type` 为 auto 时由 `effective_*` 结合环境探测结果解析，
    与原型实测口径一致：cuda → float16，cpu → int8。
    """

    device: str = "auto"
    compute_type: str = "auto"
    model: str = "large-v3"
    auto_concurrency: bool = True
    max_download: int = 8
    max_frame: int = 4
    max_summarize: int = 4

    def effective_device(self, cuda_usable: bool) -> str:
        if self.device != "auto":
            return self.device
        return "cuda" if cuda_usable else "cpu"

    def effective_compute_type(self, device: str) -> str:
        if self.compute_type != "auto":
            return self.compute_type
        return "float16" if device == "cuda" else "int8"


@dataclass
class TranscribeConfig:
    """转写参数。

    ⚠️ `vad_min_silence_ms` 与 `beam_size` 直接决定**分段边界**。
    samples/transcript_gpu.txt 这条金标准基线就是在
    `beam_size=5` + `vad_min_silence_ms=400` 下产生的，
    改动这两个值会导致验收的逐字符比对失败——那不是 bug，是参数变了。
    """

    language: str = "zh"
    beam_size: int = 5
    vad_filter: bool = True
    vad_min_silence_ms: int = 400
    initial_prompt: str = ""


@dataclass
class FramesConfig:
    """抽帧与拼版。默认关闭——实测多数口播视频画面无独立信息量（实测记录 §4）。"""

    enabled: bool = False
    scene_threshold: float = 0.3
    max_frames: int = 400
    sheet_cols: int = 6
    sheet_rows: int = 4


@dataclass
class VisionConfig:
    """画面理解（可选）：把拼版图交给视觉模型读，产出 `visual.md`。

    默认关闭 —— 它要么依赖本机的视觉模型（`local_ai`），要么依赖云端的多模态
    接口，两者都不是「装完就有」的。关掉后仍是完整可用的转写工具（PRD NFR-4）。

    为什么不复用 `frames` 的开关：抽帧是「要不要这些图片」，画面理解是
    「要不要让模型看图」。前者是产物，后者是行为，混在一个开关里会让
    「我只想要缩略图」的用户被迫去看模型脸色。
    """

    enabled: bool = False
    #: 最多看几张拼版图。每条视频通常 3~6 张（每张 24 格），全看会线性加耗时
    #: （本机实测约 1.8 s/张；云端按 token 计费，一张 ≈1500 prompt token）。
    max_sheets: int = 6
    #: 单张图允许生成的 token 上限。太小会把 OCR 结果截断，太大则 2B 容易跑飞。
    max_tokens: int = 400


@dataclass
class SummarizeConfig:
    """汇总（可选，走 OpenAI 兼容接口）。密钥不进配置文件，只留环境变量名。

    后端由 `app/core/ai_backend.py` 决定：本地 `llama-server` 优先，起不来时
    回退到这里的 `base_url`（云 API）。所以「填了 base_url 却没有 key」不再是
    死路 —— 只要本地模型在，汇总照样能出。
    """

    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "VIDIGEST_API_KEY"
    model: str = "gpt-4o"
    template: str = "摘要+章节大纲+要点"


@dataclass
class LocalAIConfig:
    """本机 llama-server（GGUF 推理）—— 「本地优先、云 API 兜底」的底座。

    为什么是 llama-server 而不是 transformers（2026-10-27 实测，见
    `docs/实测记录.md` §8）：同一个 Qwen3-VL-2B，CPU + torch 要 **45 s/张**，
    GGUF Q4 + llama.cpp 在 4060 Ti 上 **1.8 s/张**、显存 3.2 GiB。差 25 倍。
    更重要的是 llama-server 直接暴露 `/v1/chat/completions`，
    于是云端与本地在代码上是同一条路，只差一个 `base_url`。

    `enabled=True` 的含义是「**允许**用本地模型」，不是「只用本地」：
    本地起不来时自动回退到 `summarize.base_url`。要彻底离线，
    把云端的 base_url 留空、只开这一段即可。
    """

    enabled: bool = False
    #: llama-server(.exe) 的完整路径。None = 在 `<模型目录>/llama/` 下按名字找。
    server_path: str | None = None
    #: 存 GGUF 的目录。None = `<paths.model_dir>/qwen3-vl-2b`。
    model_dir: str | None = None
    #: 监听端口。**固定端口**是有意的：上次异常退出残留的 server 能直接复用，
    #: 不必每次重付 2.6 s 的模型加载。
    port: int = 8099
    #: 卸载到 GPU 的层数。99 = 全量（2B Q4 实测只需约 3.2 GiB 显存）。
    gpu_layers: int = 99
    #: 等待模型加载的上限（秒）。冷启动实测 2.6 s，给足余量防慢盘 / 杀软扫描。
    startup_timeout: float = 120.0
    #: 端口上已有健康的 llama-server 就复用（可能是上次残留的进程）。
    reuse_running: bool = True

    def resolve(self, root: Path) -> LocalAIConfig:
        """把相对路径锚定到项目根。逐字段重建 —— 新增字段必须一起带过来。"""
        server = Path(self.server_path).expanduser() if self.server_path else None
        mdir = Path(self.model_dir).expanduser() if self.model_dir else None
        return LocalAIConfig(
            enabled=self.enabled,
            server_path=str(server if server and server.is_absolute() else root / server)
            if server
            else None,
            model_dir=str(mdir if mdir and mdir.is_absolute() else root / mdir) if mdir else None,
            port=self.port,
            gpu_layers=self.gpu_layers,
            startup_timeout=self.startup_timeout,
            reuse_running=self.reuse_running,
        )


@dataclass
class CookieConfig:
    browser: str = "chromium"
    auto_refresh: bool = True


@dataclass
class DownloadConfig:
    rate_limit_per_min: int = 10
    keep_intermediate: bool = True
    #: 模型下载的镜像基址（`{model}` 会被替换）。None 表示用
    #: `app/core/model_downloader.py` 的 `DEFAULT_BASE_URL`（hf-mirror）。
    #:
    #: 为什么要能覆盖：镜像是第三方服务，失效时不该要求用户改代码或等新版本。
    #: 官方 `huggingface.co` 国内不通，所以默认值不能直接用它。
    base_url: str | None = None


@dataclass
class Config:
    """顶层配置。`root` 是配置文件所在目录，用于锚定相对路径，不写回 yaml。"""

    paths: PathsConfig = field(default_factory=PathsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    frames: FramesConfig = field(default_factory=FramesConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    local_ai: LocalAIConfig = field(default_factory=LocalAIConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    cookie: CookieConfig = field(default_factory=CookieConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    root: Path = field(default_factory=Path.cwd, repr=False, compare=False)

    # ---- 构造 ----

    @classmethod
    def from_dict(cls, data: Any, root: str | Path | None = None) -> Config:
        d = data if isinstance(data, dict) else {}
        cfg = cls(
            paths=_section(PathsConfig, d.get("paths")),
            runtime=_section(RuntimeConfig, d.get("runtime")),
            transcribe=_section(TranscribeConfig, d.get("transcribe")),
            frames=_section(FramesConfig, d.get("frames")),
            summarize=_section(SummarizeConfig, d.get("summarize")),
            local_ai=_section(LocalAIConfig, d.get("local_ai")),
            vision=_section(VisionConfig, d.get("vision")),
            cookie=_section(CookieConfig, d.get("cookie")),
            download=_section(DownloadConfig, d.get("download")),
            root=Path(root) if root else Path.cwd(),
        )
        cfg.paths = cfg.paths.resolve(cfg.root)
        cfg.local_ai = cfg.local_ai.resolve(cfg.root)
        return cfg

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f"配置文件不存在：{p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML 解析失败：{p}\n{exc}") from exc
        cfg = cls.from_dict(raw, root=p.parent)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """加载指定配置；未指定则用项目根下的 config.yaml。文件缺失即报错。"""
        p = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
        if not p.is_file():
            raise ConfigError(
                f"配置文件不存在：{p}\n"
                f"先执行：cp {EXAMPLE_CONFIG_NAME} {DEFAULT_CONFIG_NAME}"
            )
        return cls.from_file(p)

    @classmethod
    def load_or_default(
        cls, path: str | Path | None = None, root: str | Path | None = None
    ) -> tuple[Config, bool]:
        """配置缺失时回退到默认值，返回 (配置, 是否使用了默认值)。

        这样 clone 下来没有 config.yaml 也能直接跑（用相对路径默认值），
        符合「下载即可运行」的目标。

        `root` **只在回退分支生效**（文件存在时锚点是配置文件自己的目录，
        不能让调用方覆盖 —— 否则 `config.yaml` 里的相对路径会锚到别处）。
        打包运行（PyInstaller）时由 `core.paths.app_root()` 传入可写的数据目录：
        默认的 `Path.cwd()` 此时是安装目录，Windows 上不可写。
        """
        p = Path(path) if path else Path(DEFAULT_CONFIG_NAME)
        if p.is_file():
            return cls.from_file(p), False
        return cls.from_dict({}, root=Path(root) if root else Path.cwd()), True

    # ---- 输出 ----

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("root", None)
        return data

    def save(self, path: str | Path) -> None:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ---- 派生 ----

    def model_path(self) -> Path:
        """模型本地目录。不存在时由调用方给出下载指引，不做隐式联网下载。"""
        return Path(self.paths.model_dir) / self.runtime.model

    def vision_model_path(self) -> Path:
        """视觉模型（GGUF + mmproj）的落点。

        与 `model_path()` 并列而不是塞进 `models/<runtime.model>/`：
        whisper 与视觉模型的生命周期不同（前者必装、后者可选），
        混在一个目录里会让「删掉视觉模型」变成一件危险的操作。
        """
        if self.local_ai.model_dir:
            return Path(self.local_ai.model_dir)
        return Path(self.paths.model_dir) / "qwen3-vl-2b"

    def work_path(self) -> Path:
        return Path(self.paths.work_dir)

    # ---- 校验 ----

    def validate(self) -> None:
        if self.runtime.device not in VALID_DEVICES:
            raise ConfigError(
                f"runtime.device 非法：{self.runtime.device!r}，可选 {VALID_DEVICES}"
            )
        lang = self.transcribe.language
        if lang != LANGUAGE_AUTO and lang not in SUPPORTED_LANGUAGES:
            # 早失败：否则错误要等到真跑转写、模型加载完才以
            # `ValueError: Language 'xx' is not supported` 的形式出现，
            # 那时用户已经在等结果了。
            raise ConfigError(
                f"transcribe.language 非法：{lang!r}，留空表示自动检测，"
                f"或取 {len(SUPPORTED_LANGUAGES)} 个语言码之一（如 zh / en / ja）"
            )
        if self.runtime.compute_type not in VALID_COMPUTE_TYPES:
            raise ConfigError(
                f"runtime.compute_type 非法：{self.runtime.compute_type!r}，"
                f"可选 {VALID_COMPUTE_TYPES}"
            )
        if self.transcribe.beam_size < 1:
            raise ConfigError(f"transcribe.beam_size 必须 >= 1，当前 {self.transcribe.beam_size}")
        if self.transcribe.vad_min_silence_ms < 0:
            raise ConfigError(
                f"transcribe.vad_min_silence_ms 必须 >= 0，当前 {self.transcribe.vad_min_silence_ms}"
            )
        if self.frames.sheet_cols < 1 or self.frames.sheet_rows < 1:
            raise ConfigError("frames.sheet_cols / sheet_rows 必须 >= 1")
        if self.download.rate_limit_per_min < 1:
            raise ConfigError("download.rate_limit_per_min 必须 >= 1")
        if not 1024 <= self.local_ai.port <= 65535:
            raise ConfigError(f"local_ai.port 必须在 1024–65535，当前 {self.local_ai.port}")
        if self.local_ai.startup_timeout <= 0:
            raise ConfigError("local_ai.startup_timeout 必须 > 0")
        if self.vision.max_sheets < 1:
            raise ConfigError("vision.max_sheets 必须 >= 1")
        if self.vision.max_tokens < 64:
            # 低于 64 时 OCR 结果必然被截断，产物会变成半句话，不如早点拦住
            raise ConfigError("vision.max_tokens 必须 >= 64")
