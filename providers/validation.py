"""
Runtime provider validation — checks that a provider is correctly
configured and its endpoint is reachable BEFORE the first chat call.

Returns a `ValidationReport` with pass/fail/warn checks.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CheckResult:
    """One validation check."""
    name: str
    passed: bool
    message: str = ""
    level: str = "error"  # "error" | "warning" | "info"


@dataclass
class ValidationReport:
    """Aggregated result of provider validation."""
    provider_name: str
    status: str = "ok"  # "ok" | "warning" | "error"
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.level == "error")

    @property
    def errors(self) -> list[CheckResult]:
        return [c for c in self.checks if c.level == "error" and not c.passed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.level == "warning" and not c.passed]


def validate_provider(
    provider_name: str,
    api_key: str,
    base_url: str,
    model_name: str,
    context_window: int,
) -> ValidationReport:
    """
    Perform pre-flight validation of a provider configuration.

    Checks performed:
      1. API key is set (non-empty).
      2. API key format is roughly valid (starts with expected prefix).
      3. Base URL is a valid-looking HTTPS URL.
      4. Model name is non-empty.
      5. Context window is at least 4096 (minimum for useful chat).

    Does NOT make a network call — that's done by
    `BaseProvider.test_connection()` post-instantiation.

    Returns a `ValidationReport` — use `.all_passed` to check
    whether the provider is safe to use.
    """
    report = ValidationReport(provider_name=provider_name)

    # --- 1. API key presence ---
    if not api_key or api_key.strip() == "":
        report.checks.append(CheckResult(
            name="api_key_present",
            passed=False,
            message="API key is empty — set ${PROVIDER}_API_KEY in .env",
            level="error",
        ))
    else:
        report.checks.append(CheckResult(
            name="api_key_present",
            passed=True,
            message="API key is set",
        ))

    # --- 2. API key format (heuristic) ---
    if api_key:
        if provider_name == "deepseek" and not api_key.startswith("sk-"):
            report.checks.append(CheckResult(
                name="api_key_format",
                passed=False,
                message="DeepSeek keys usually start with 'sk-'. Check your key.",
                level="warning",
            ))
        elif provider_name == "anthropic" and not api_key.startswith("sk-ant-"):
            report.checks.append(CheckResult(
                name="api_key_format",
                passed=False,
                message="Anthropic keys usually start with 'sk-ant-'. Check your key.",
                level="warning",
            ))
        else:
            report.checks.append(CheckResult(
                name="api_key_format",
                passed=True,
                message="API key format looks valid",
            ))

    # --- 3. Base URL ---
    if not base_url:
        report.checks.append(CheckResult(
            name="base_url",
            passed=False,
            message="Base URL is empty",
            level="error",
        ))
    elif not (base_url.startswith("http://") or base_url.startswith("https://")):
        report.checks.append(CheckResult(
            name="base_url",
            passed=False,
            message=f"Base URL '{base_url}' should start with http:// or https://",
            level="error",
        ))
    else:
        report.checks.append(CheckResult(
            name="base_url",
            passed=True,
            message=f"Base URL: {base_url}",
        ))

    # --- 4. Model name ---
    if not model_name:
        report.checks.append(CheckResult(
            name="model_name",
            passed=False,
            message="Model name is empty — set model.model_name in config.yaml",
            level="error",
        ))
    else:
        report.checks.append(CheckResult(
            name="model_name",
            passed=True,
            message=f"Model: {model_name}",
        ))

    # --- 5. Context window sanity ---
    if context_window < 4096:
        report.checks.append(CheckResult(
            name="context_window",
            passed=False,
            message=f"Context window {context_window} is too small (min 4096)",
            level="error",
        ))
    elif context_window < 16384:
        report.checks.append(CheckResult(
            name="context_window",
            passed=False,
            message=f"Context window {context_window} is on the small side (< 16384)",
            level="warning",
        ))
    else:
        report.checks.append(CheckResult(
            name="context_window",
            passed=True,
            message=f"Context window: {context_window}",
        ))

    # Determine overall status
    if any(not c.passed and c.level == "error" for c in report.checks):
        report.status = "error"
    elif any(not c.passed and c.level == "warning" for c in report.checks):
        report.status = "warning"
    else:
        report.status = "ok"

    return report
