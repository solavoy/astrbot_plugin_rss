from __future__ import annotations

import re
from typing import Any


class BrowserCompatibilityManager:
    """Reports observed browser installation and curl_cffi target compatibility."""

    def __init__(self, configured_version: str | None = None) -> None:
        self.configured_version = configured_version

    def inspect(self) -> dict[str, Any]:
        try:
            from cloakbrowser import binary_info

            info = binary_info(browser_version=self.configured_version)
            browser_version = info.get("installed_version") if info.get("installed") else None
            browser_version = browser_version or (info.get("version") if info.get("installed") else None)
        except Exception as exc:
            info = {"installed": False, "error": type(exc).__name__}
            browser_version = None
        try:
            from typing import get_args

            from curl_cffi.requests.impersonate import BrowserTypeLiteral

            targets = get_args(BrowserTypeLiteral)
        except Exception:
            targets = ()
        target = None
        if browser_version:
            major = re.match(r"(\d+)", str(browser_version))
            candidate = f"chrome{major.group(1)}" if major else ""
            if candidate in targets:
                target = candidate
        compatible = bool(info.get("installed") and target)
        return {
            "browser_installed": bool(info.get("installed")),
            "browser_version": browser_version,
            "configured_version": self.configured_version,
            "binary_path": info.get("binary_path"),
            "curl_targets": sorted(str(t) for t in targets),
            "impersonate_target": target,
            "compatible": compatible,
            "compatibility_level": "compatible_major" if compatible else "unavailable_or_incompatible",
            "status": "compatible_major" if compatible else "browser_unavailable_or_incompatible",
        }
