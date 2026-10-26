"""Service-level settings (deployment knobs). Engine tunables live in
clause_splitter/config.py; this module only holds what the service layer needs."""

import os
from dataclasses import dataclass
from pathlib import Path

from clause_splitter.config import REPO_ROOT


@dataclass(frozen=True)
class ConfigApp:
    data_dir: Path
    api_key: str
    cors_origins: list[str]
    job_workers: int
    max_upload_mb: int
    applicability_max_chars: int
    log_level: str
    company_profile: str
    backend_callback_url: str

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


DEFAULT_COMPANY_PROFILE = (
    "平安科技是平安集团旗下的科技公司,负责集团及成员公司的科技能力建设。"
    "业务涵盖:软件研发与运维、数据平台与大数据处理、人工智能与算法研发、"
    "云计算与基础设施、信息安全、研发效能与智能体平台等;服务对象为平安集团及"
    "保险、银行、投资等成员公司,属于金融行业的科技支撑主体。"
)

CONFIG_APP = ConfigApp(
    data_dir=Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data"))),
    api_key=os.environ.get("API_KEY", ""),
    cors_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    # 1 keeps chains serial so total LLM fan-out = COMPARE_CONCURRENCY
    # (the intranet qwen3.8-27b only has 2 concurrent slots).
    job_workers=int(os.environ.get("JOB_WORKERS", "1")),
    max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "200")),
    applicability_max_chars=int(os.environ.get("APPLICABILITY_MAX_CHARS", "30000")),
    log_level=os.environ.get("LOG_LEVEL", "INFO"),
    company_profile=os.environ.get("COMPANY_PROFILE", DEFAULT_COMPANY_PROFILE),
    backend_callback_url=os.environ.get("BACKEND_CALLBACK_URL", ""),
)
